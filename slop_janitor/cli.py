from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from slop_janitor.app_server import AppServerClient
from slop_janitor.app_server import AppServerError
from slop_janitor.app_server import AppServerSpawnSpec
from slop_janitor.models import Stage
from slop_janitor.models import TokenUsageSnapshot
from slop_janitor.models import TokenUsageSummary
from slop_janitor.run_log import DEFAULT_RUNS_DIR
from slop_janitor.run_log import RunLogger
from slop_janitor.run_log import build_run_log_path


LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
CODEX_CLI_PREFIX = (
    "cargo",
    "run",
    "-q",
    "-p",
    "codex-cli",
    "--bin",
    "codex",
    "--",
)
CLIENT_VERSION = "0.1.0"

SKILL_PATHS = {
    "execplan-create": SKILLS_ROOT / "execplan-create" / "SKILL.md",
    "execplan-improve": SKILLS_ROOT / "execplan-improve" / "SKILL.md",
    "find-best-refactor": SKILLS_ROOT / "find-best-refactor" / "SKILL.md",
    "implement-execplan": SKILLS_ROOT / "implement-execplan" / "SKILL.md",
    "review-recent-work": SKILLS_ROOT / "review-recent-work" / "SKILL.md",
}


@dataclass(frozen=True)
class AutoCommitState:
    enabled: bool
    repo_root: Path
    excluded_relative_paths: tuple[str, ...] = ()


def stage_label(base_label: str, *, cycle_index: int, cycles: int) -> str:
    if cycles == 1:
        return base_label
    return f"cycle-{cycle_index}-{base_label}"


def build_follow_up_stages(*, cycle_index: int, cycles: int, improvement_count: int, review_count: int) -> list[Stage]:
    return [
        *[
            Stage(
                label=stage_label(f"execplan-improve-{index}", cycle_index=cycle_index, cycles=cycles),
                skill_name="execplan-improve",
                skill_path=str(SKILL_PATHS["execplan-improve"]),
                text="$execplan-improve improve the pending execution plan at .agent/execplan-pending.md",
            )
            for index in range(1, improvement_count + 1)
        ],
        Stage(
            label=stage_label("implement-execplan", cycle_index=cycle_index, cycles=cycles),
            skill_name="implement-execplan",
            skill_path=str(SKILL_PATHS["implement-execplan"]),
            text="$implement-execplan implement the pending execution plan at .agent/execplan-pending.md",
        ),
        *[
            Stage(
                label=stage_label(f"review-recent-work-{index}", cycle_index=cycle_index, cycles=cycles),
                skill_name="review-recent-work",
                skill_path=str(SKILL_PATHS["review-recent-work"]),
                text="$review-recent-work review the most recently implemented ExecPlan work",
            )
            for index in range(1, review_count + 1)
        ],
    ]


def build_pipeline_stages(prompt: str, *, cycles: int, improvement_count: int, review_count: int) -> list[Stage]:
    stages: list[Stage] = []
    for cycle_index in range(1, cycles + 1):
        stages.append(
            Stage(
                label=stage_label("execplan-create", cycle_index=cycle_index, cycles=cycles),
                skill_name="execplan-create",
                skill_path=str(SKILL_PATHS["execplan-create"]),
                text=f"$execplan-create {prompt}",
            )
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=cycle_index,
                cycles=cycles,
                improvement_count=improvement_count,
                review_count=review_count,
            )
        )
    return stages


def build_refactor_stages(
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
) -> list[Stage]:
    text = "$find-best-refactor"
    if prompt:
        text = f"{text} {prompt}"
    else:
        text = f"{text} find the single highest-leverage refactor in this repository"
    stages: list[Stage] = []
    for cycle_index in range(1, cycles + 1):
        stages.append(
            Stage(
                label=stage_label("find-best-refactor", cycle_index=cycle_index, cycles=cycles),
                skill_name="find-best-refactor",
                skill_path=str(SKILL_PATHS["find-best-refactor"]),
                text=text,
            )
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=cycle_index,
                cycles=cycles,
                improvement_count=improvement_count,
                review_count=review_count,
            )
        )
    return stages


def validate_counts(*, cycles: int, improvement_count: int, review_count: int) -> None:
    if cycles < 1:
        raise AppServerError("`--cycles` must be at least 1")
    if improvement_count < 0:
        raise AppServerError("`--improvements` must be 0 or greater")
    if review_count < 0:
        raise AppServerError("`--review` must be 0 or greater")


def build_stages(
    mode: str,
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
) -> list[Stage]:
    validate_counts(cycles=cycles, improvement_count=improvement_count, review_count=review_count)
    if mode == "pipeline":
        if not prompt:
            raise AppServerError("`--prompt` is required when `--mode pipeline` is selected")
        return build_pipeline_stages(
            prompt,
            cycles=cycles,
            improvement_count=improvement_count,
            review_count=review_count,
        )
    if mode == "refactor":
        return build_refactor_stages(
            prompt,
            cycles=cycles,
            improvement_count=improvement_count,
            review_count=review_count,
        )
    raise AppServerError(f"unsupported mode: {mode}")


def resolve_codex_workspace(cli_value: str | None) -> Path:
    raw_value = cli_value or os.environ.get("CODEX_WORKSPACE")
    if not raw_value:
        raise AppServerError(
            "Codex workspace is not configured. Pass `--codex-workspace /path/to/codex-rs` or set `CODEX_WORKSPACE`."
        )
    return Path(raw_value).expanduser()


def default_app_server_spawn_spec(codex_workspace: Path) -> AppServerSpawnSpec:
    return AppServerSpawnSpec(argv=(*CODEX_CLI_PREFIX, "app-server"), cwd=str(codex_workspace))


def default_codex_cli_spawn_spec(codex_workspace: Path) -> AppServerSpawnSpec:
    return AppServerSpawnSpec(argv=CODEX_CLI_PREFIX, cwd=str(codex_workspace))


def validate_workspace(codex_workspace: Path) -> None:
    if not codex_workspace.is_dir():
        raise AppServerError(f"Codex workspace is missing: {codex_workspace}")


def validate_cargo() -> None:
    if shutil.which("cargo") is None:
        raise AppServerError("`cargo` is required in PATH for the default Codex launch path")


def validate_skills(stages: list[Stage]) -> None:
    if not stages:
        raise AppServerError("expected at least one stage")
    for stage in stages:
        if not Path(stage.skill_path).is_file():
            raise AppServerError(f"required skill path is missing: {stage.skill_path}")


def format_token_usage(snapshot: TokenUsageSnapshot) -> str:
    return (
        f"total={snapshot.total_tokens} "
        f"input={snapshot.input_tokens} "
        f"cached={snapshot.cached_input_tokens} "
        f"output={snapshot.output_tokens} "
        f"reasoning={snapshot.reasoning_output_tokens}"
    )


def write_token_footer(run_logger: RunLogger, summary: TokenUsageSummary) -> None:
    run_logger.write_line(
        f"Tokens this turn: {format_token_usage(summary.last)}",
        to_terminal=True,
    )
    run_logger.write_line(
        f"Tokens cumulative: {format_token_usage(summary.total)}",
        to_terminal=True,
    )


def extract_root_config_args(args: list[str]) -> tuple[list[str], list[str]]:
    root_args: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--config="):
            root_args.append(token)
            index += 1
            continue
        if token in {"-c", "--config"}:
            if index + 1 >= len(args):
                raise AppServerError(f"{token} requires a key=value argument")
            root_args.extend([token, args[index + 1]])
            index += 2
            continue
        remaining.append(token)
        index += 1
    return root_args, remaining


def build_auth_command(base_argv: tuple[str, ...], argv: list[str]) -> list[str]:
    if not argv:
        raise AppServerError("usage: slop-janitor auth <login|status|logout> [args]")
    verb = argv[0]
    extras = argv[1:]
    root_args, remaining = extract_root_config_args(extras)
    if verb == "login":
        return [*base_argv, *root_args, "login", *remaining]
    if verb == "status":
        return [*base_argv, *root_args, "login", "status", *remaining]
    if verb == "logout":
        return [*base_argv, *root_args, "logout", *remaining]
    raise AppServerError(f"unsupported auth command: {verb}")


def run_auth(
    argv: list[str],
    *,
    codex_workspace: Path | None = None,
    codex_cli_spawn_spec: AppServerSpawnSpec | None = None,
) -> int:
    if codex_cli_spawn_spec is None:
        if codex_workspace is None:
            raise AppServerError("Codex workspace is required when no auth spawn override is provided")
        validate_workspace(codex_workspace)
        validate_cargo()
        spawn_spec = default_codex_cli_spawn_spec(codex_workspace)
    else:
        spawn_spec = codex_cli_spawn_spec
    command = build_auth_command(spawn_spec.argv, argv)
    LOGGER.info("running auth command: %s", " ".join(command))
    completed = subprocess.run(command, cwd=spawn_spec.cwd, check=False)
    return completed.returncode


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slop-janitor")
    parser.add_argument("--codex-workspace")
    parser.add_argument("--mode", choices=("pipeline", "refactor"), default="pipeline")
    parser.add_argument("--prompt")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--improvements", type=int, default=4)
    parser.add_argument("--review", type=int, default=5)
    return parser


def build_auth_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slop-janitor auth")
    parser.add_argument("--codex-workspace")
    parser.add_argument("auth_args", nargs=argparse.REMAINDER)
    return parser


def create_run_logger(*, runs_dir: Path, run_cwd: Path, mode: str, prompt: str | None) -> RunLogger:
    log_path = build_run_log_path(runs_dir, run_cwd)
    try:
        return RunLogger(log_path, run_cwd=run_cwd, mode=mode, prompt=prompt)
    except OSError as exc:
        raise AppServerError(f"failed to create run log at {log_path}: {exc}") from exc


def git_status_has_changes(repo_root: Path, excluded_relative_paths: tuple[str, ...] = ()) -> bool | None:
    command = ["git", "status", "--short", "--", ".", *[f":(exclude){path}" for path in excluded_relative_paths]]
    status = subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return None
    return bool(status.stdout.strip())


def git_add_all(repo_root: Path, excluded_relative_paths: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    command = ["git", "add", "-A", "--", ".", *[f":(exclude){path}" for path in excluded_relative_paths]]
    return subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def prepare_auto_commit_state(run_cwd: Path, run_logger: RunLogger) -> AutoCommitState:
    if shutil.which("git") is None:
        run_logger.write_line("[commit] auto-commit disabled: `git` is not available")
        return AutoCommitState(enabled=False, repo_root=run_cwd)
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=run_cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        run_logger.write_line("[commit] auto-commit disabled: target directory is not inside a git repository")
        return AutoCommitState(enabled=False, repo_root=run_cwd)
    repo_root = Path(probe.stdout.strip())
    repo_root_resolved = repo_root.resolve(strict=False)
    excluded_relative_paths: tuple[str, ...] = ()
    try:
        log_relative_path = run_logger.log_path.resolve(strict=False).relative_to(repo_root_resolved)
        excluded_relative_paths = (log_relative_path.as_posix(),)
    except ValueError:
        excluded_relative_paths = ()
    has_changes = git_status_has_changes(repo_root, excluded_relative_paths)
    if has_changes is None:
        run_logger.write_line("[commit] auto-commit disabled: failed to inspect git status")
        return AutoCommitState(enabled=False, repo_root=repo_root)
    if has_changes:
        run_logger.write_line("[commit] auto-commit disabled: repository had pre-existing changes at start")
        return AutoCommitState(enabled=False, repo_root=repo_root)
    run_logger.write_line(f"[commit] auto-commit enabled for {repo_root}")
    return AutoCommitState(
        enabled=True,
        repo_root=repo_root,
        excluded_relative_paths=excluded_relative_paths,
    )


def maybe_commit_checkpoint(auto_commit: AutoCommitState, run_logger: RunLogger, message: str) -> None:
    if not auto_commit.enabled:
        return
    has_changes = git_status_has_changes(auto_commit.repo_root, auto_commit.excluded_relative_paths)
    if has_changes is None:
        run_logger.write_line("[commit] skipping checkpoint: failed to inspect git status")
        return
    if not has_changes:
        run_logger.write_line(f"[commit] skipping `{message}`: no changes to commit")
        return
    add_result = git_add_all(auto_commit.repo_root, auto_commit.excluded_relative_paths)
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip() or "git add failed"
        run_logger.write_line(f"[commit] failed `{message}`: {detail}", to_terminal=True, stream="stderr")
        return
    commit_result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=auto_commit.repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        detail = (commit_result.stderr or commit_result.stdout).strip() or "git commit failed"
        run_logger.write_line(f"[commit] failed `{message}`: {detail}", to_terminal=True, stream="stderr")
        return
    run_logger.write_line(f"[commit] created `{message}`")


def maybe_commit_for_stage(
    auto_commit: AutoCommitState,
    run_logger: RunLogger,
    stage: Stage,
    *,
    stage_index: int,
) -> None:
    if stage_index == 1:
        maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: initial plan created")
        return
    if stage.skill_name == "implement-execplan":
        maybe_commit_checkpoint(auto_commit, run_logger, f"slop-janitor: after {stage.label}")


def run(
    argv: list[str] | None = None,
    *,
    spawn_spec: AppServerSpawnSpec | None = None,
    runs_dir: Path | None = None,
) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    client: AppServerClient | None = None
    run_logger: RunLogger | None = None
    auto_commit: AutoCommitState | None = None
    try:
        run_cwd = Path.cwd()
        run_logger = create_run_logger(
            runs_dir=runs_dir or DEFAULT_RUNS_DIR,
            run_cwd=run_cwd,
            mode=args.mode,
            prompt=args.prompt,
        )
        run_logger.write_line(f"cycles={args.cycles}")
        run_logger.write_line(f"improvements={args.improvements}")
        run_logger.write_line(f"review={args.review}")
        run_logger.write_line("")
        auto_commit = prepare_auto_commit_state(run_cwd, run_logger)
        stages = build_stages(
            args.mode,
            args.prompt,
            cycles=args.cycles,
            improvement_count=args.improvements,
            review_count=args.review,
        )
        validate_skills(stages)

        if spawn_spec is None:
            codex_workspace = resolve_codex_workspace(args.codex_workspace)
            validate_workspace(codex_workspace)
            validate_cargo()
            client_spawn_spec = default_app_server_spawn_spec(codex_workspace)
        else:
            client_spawn_spec = spawn_spec

        client = AppServerClient(client_spawn_spec, run_logger)
        client.start()
        client.initialize()
        account_info = client.get_account()
        if account_info.get("requiresOpenaiAuth") and account_info.get("account") is None:
            run_logger.write_line(
                "OpenAI auth is required before starting the pipeline. Run `./slop-janitor auth login`.",
                to_terminal=True,
                stream="stderr",
            )
            return 1

        thread_id = client.start_thread(str(run_cwd))
        for index, stage in enumerate(stages, start=1):
            run_logger.write_line(f"=== Stage {index}/{len(stages)}: {stage.label} ===")
            result = client.run_turn(thread_id, stage)
            if result.token_usage is not None:
                write_token_footer(run_logger, result.token_usage)
            if result.status != "completed":
                message = result.error_message or "unknown turn failure"
                run_logger.write_line(
                    f"Stage {stage.label} failed: {message}",
                    to_terminal=True,
                    stream="stderr",
                )
                return 1
            if result.token_usage is None:
                run_logger.write_line(
                    f"Stage {stage.label} failed: successful turn completed without token usage data",
                    to_terminal=True,
                    stream="stderr",
                )
                return 1
            maybe_commit_for_stage(auto_commit, run_logger, stage, stage_index=index)
        maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: final checkpoint")
        return 0
    except AppServerError as exc:
        if run_logger is not None:
            run_logger.write_line(str(exc), to_terminal=True, stream="stderr")
        else:
            print(str(exc), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        if run_logger is not None:
            run_logger.close()


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "auth":
        try:
            auth_args = build_auth_parser().parse_args(raw_argv[1:])
            if not auth_args.auth_args:
                raise AppServerError("usage: slop-janitor auth <login|status|logout> [args]")
            build_auth_command((), auth_args.auth_args)
            codex_workspace = resolve_codex_workspace(auth_args.codex_workspace)
            return run_auth(auth_args.auth_args, codex_workspace=codex_workspace)
        except AppServerError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return run(raw_argv)

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from slop_janitor.app_server import AppServerClient
from slop_janitor.app_server import AppServerError
from slop_janitor.app_server import AppServerRequestError
from slop_janitor.app_server import AppServerSpawnSpec
from slop_janitor.app_server import AppServerTimeoutError
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
DEFAULT_STAGE_IDLE_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_STAGE_RETRIES = 6
DEFAULT_RETRY_INITIAL_DELAY_SECONDS = 15.0
DEFAULT_RETRY_MAX_DELAY_SECONDS = 300.0
IMPROVE_SKILL_CHOICES = (
    "execplan-improve",
    "execplan-improve-subagents",
)
REVIEW_SKILL_CHOICES = (
    "review-recent-work",
    "review-recent-work-subagents",
)
SANDBOX_MODE_CHOICES = (
    "workspace-write",
    "danger-full-access",
)
DEFAULT_REFACTOR_PROMPT = "identify the top materially different refactor candidates in this repository"

SKILL_PATHS = {
    "find-refactor-candidates": SKILLS_ROOT / "find-refactor-candidates" / "SKILL.md",
    "select-refactor": SKILLS_ROOT / "select-refactor" / "SKILL.md",
    "execplan-create": SKILLS_ROOT / "execplan-create" / "SKILL.md",
    "execplan-improve": SKILLS_ROOT / "execplan-improve" / "SKILL.md",
    "execplan-improve-subagents": SKILLS_ROOT / "execplan-improve-subagents" / "SKILL.md",
    "implement-execplan": SKILLS_ROOT / "implement-execplan" / "SKILL.md",
    "review-recent-work": SKILLS_ROOT / "review-recent-work" / "SKILL.md",
    "review-recent-work-subagents": SKILLS_ROOT / "review-recent-work-subagents" / "SKILL.md",
}


@dataclass(frozen=True)
class AutoCommitState:
    enabled: bool
    repo_root: Path
    excluded_relative_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecPlanSnapshot:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class WorkflowArtifactSnapshot:
    path: str | None
    fingerprint: "FileFingerprint"


@dataclass(frozen=True)
class FileFingerprint:
    exists: bool
    size: int
    sha256: str | None


@dataclass(frozen=True)
class RepoStateSnapshot:
    repo_root: Path
    head_commit: str | None
    status_lines: tuple[str, ...]


@dataclass(frozen=True)
class StageWorkspaceSnapshot:
    repo_states: tuple[RepoStateSnapshot, ...]
    tracked_artifacts: tuple[WorkflowArtifactSnapshot, ...]


@dataclass(frozen=True)
class FailureAssessment:
    retryable: bool
    restart_client: bool
    reason: str


@dataclass(frozen=True)
class StageExecutionOutcome:
    client: AppServerClient
    thread_id: str
    token_usage: TokenUsageSummary | None
    recovered_via_postconditions: bool = False


class RunStateTracker:
    def __init__(self, path: Path, *, run_cwd: Path, mode: str, prompt: str | None) -> None:
        self.path = path
        self._payload: dict[str, Any] = {
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "cwd": str(run_cwd),
            "mode": mode,
            "prompt": prompt,
            "status": "starting",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()

    def update(self, **fields: Any) -> None:
        self._payload.update(fields)
        self._write()

    def close(self, *, status: str) -> None:
        self.update(status=status, endedAt=datetime.now(timezone.utc).isoformat())

    def _write(self) -> None:
        self.path.write_text(json.dumps(self._payload, indent=2, sort_keys=True), encoding="utf-8")


def stage_label(base_label: str, *, cycle_index: int, cycles: int) -> str:
    if cycles == 1:
        return base_label
    return f"cycle-{cycle_index}-{base_label}"


def build_follow_up_stages(
    *,
    cycle_index: int,
    cycles: int,
    improvement_count: int,
    review_count: int,
    improve_skill_name: str,
    review_skill_name: str,
) -> list[Stage]:
    return [
        *[
            Stage(
                label=stage_label(f"{improve_skill_name}-{index}", cycle_index=cycle_index, cycles=cycles),
                skill_name=improve_skill_name,
                skill_path=str(SKILL_PATHS[improve_skill_name]),
                text=f"${improve_skill_name} improve the active work-item ExecPlan and rewrite it in place",
            )
            for index in range(1, improvement_count + 1)
        ],
        Stage(
            label=stage_label("implement-execplan", cycle_index=cycle_index, cycles=cycles),
            skill_name="implement-execplan",
            skill_path=str(SKILL_PATHS["implement-execplan"]),
            text="$implement-execplan implement the active work-item ExecPlan",
        ),
        *[
            Stage(
                label=stage_label(f"{review_skill_name}-{index}", cycle_index=cycle_index, cycles=cycles),
                skill_name=review_skill_name,
                skill_path=str(SKILL_PATHS[review_skill_name]),
                text=f"${review_skill_name} review the most recently implemented work-item ExecPlan",
            )
            for index in range(1, review_count + 1)
        ],
    ]


def build_pipeline_stages(
    prompt: str,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
    improve_skill_name: str,
    review_skill_name: str,
) -> list[Stage]:
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
                improve_skill_name=improve_skill_name,
                review_skill_name=review_skill_name,
            )
        )
    return stages


def build_refactor_stages(
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
    improve_skill_name: str,
    review_skill_name: str,
) -> list[Stage]:
    stages: list[Stage] = []
    for cycle_index in range(1, cycles + 1):
        refactor_prompt = prompt or DEFAULT_REFACTOR_PROMPT
        stages.extend(
            [
                Stage(
                    label=stage_label("find-refactor-candidates", cycle_index=cycle_index, cycles=cycles),
                    skill_name="find-refactor-candidates",
                    skill_path=str(SKILL_PATHS["find-refactor-candidates"]),
                    text=f"$find-refactor-candidates {refactor_prompt}",
                ),
                Stage(
                    label=stage_label("select-refactor", cycle_index=cycle_index, cycles=cycles),
                    skill_name="select-refactor",
                    skill_path=str(SKILL_PATHS["select-refactor"]),
                    text=(
                        "$select-refactor pressure-test the active shortlist, lock the best refactor decision, "
                        "and stop before planning."
                    ),
                ),
                Stage(
                    label=stage_label("execplan-create", cycle_index=cycle_index, cycles=cycles),
                    skill_name="execplan-create",
                    skill_path=str(SKILL_PATHS["execplan-create"]),
                    text=(
                        "$execplan-create create an ExecPlan for the active refactor work item and write it into "
                        "that work item"
                    ),
                ),
            ]
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=cycle_index,
                cycles=cycles,
                improvement_count=improvement_count,
                review_count=review_count,
                improve_skill_name=improve_skill_name,
                review_skill_name=review_skill_name,
            )
        )
    return stages


def validate_counts(
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
    delay_between_cycles_minutes: float = 0.0,
    stage_idle_timeout_seconds: float = DEFAULT_STAGE_IDLE_TIMEOUT_SECONDS,
    max_stage_retries: int = DEFAULT_MAX_STAGE_RETRIES,
    retry_initial_delay_seconds: float = DEFAULT_RETRY_INITIAL_DELAY_SECONDS,
    retry_max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS,
) -> None:
    if cycles < 1:
        raise AppServerError("`--cycles` must be at least 1")
    if improvement_count < 0:
        raise AppServerError("`--improvements` must be 0 or greater")
    if review_count < 0:
        raise AppServerError("`--review` must be 0 or greater")
    if delay_between_cycles_minutes < 0:
        raise AppServerError("`--delay-between-cycles-minutes` must be 0 or greater")
    if stage_idle_timeout_seconds <= 0:
        raise AppServerError("`--stage-idle-timeout-seconds` must be greater than 0")
    if max_stage_retries < 0:
        raise AppServerError("`--max-stage-retries` must be 0 or greater")
    if retry_initial_delay_seconds <= 0:
        raise AppServerError("`--retry-initial-delay-seconds` must be greater than 0")
    if retry_max_delay_seconds <= 0:
        raise AppServerError("`--retry-max-delay-seconds` must be greater than 0")
    if retry_max_delay_seconds < retry_initial_delay_seconds:
        raise AppServerError("`--retry-max-delay-seconds` must be at least `--retry-initial-delay-seconds`")


def build_stages(
    mode: str,
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
    improve_skill_name: str,
    review_skill_name: str,
) -> list[Stage]:
    validate_counts(
        cycles=cycles,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    if mode == "pipeline":
        if not prompt:
            raise AppServerError("`--prompt` is required when `--mode pipeline` is selected")
        return build_pipeline_stages(
            prompt,
            cycles=cycles,
            improvement_count=improvement_count,
            review_count=review_count,
            improve_skill_name=improve_skill_name,
            review_skill_name=review_skill_name,
        )
    if mode == "refactor":
        return build_refactor_stages(
            prompt,
            cycles=cycles,
            improvement_count=improvement_count,
            review_count=review_count,
            improve_skill_name=improve_skill_name,
            review_skill_name=review_skill_name,
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
    run_logger.write_line("", to_terminal=True)
    run_logger.write_line(
        f"Tokens this turn: {format_token_usage(summary.last)}",
        to_terminal=True,
    )
    run_logger.write_line(
        f"Tokens cumulative: {format_token_usage(summary.total)}",
        to_terminal=True,
    )
    run_logger.write_line("", to_terminal=True)


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
    parser.add_argument(
        "--linked-repo",
        action="append",
        default=[],
        help="Additional git repository to manage and make writable during the run. Repeatable.",
    )
    parser.add_argument("--sandbox", choices=SANDBOX_MODE_CHOICES, default="workspace-write")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--improvements", type=int, default=1)
    parser.add_argument("--improve-skill", choices=IMPROVE_SKILL_CHOICES, default="execplan-improve")
    parser.add_argument("--review", type=int, default=1)
    parser.add_argument("--review-skill", choices=REVIEW_SKILL_CHOICES, default="review-recent-work")
    parser.add_argument("--delay-between-cycles-minutes", type=float, default=0.0)
    parser.add_argument("--stage-idle-timeout-seconds", type=float, default=DEFAULT_STAGE_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--max-stage-retries", type=int, default=DEFAULT_MAX_STAGE_RETRIES)
    parser.add_argument("--retry-initial-delay-seconds", type=float, default=DEFAULT_RETRY_INITIAL_DELAY_SECONDS)
    parser.add_argument("--retry-max-delay-seconds", type=float, default=DEFAULT_RETRY_MAX_DELAY_SECONDS)
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
    lines = git_status_lines(repo_root, excluded_relative_paths)
    if lines is None:
        return None
    return bool(lines)


def git_status_lines(repo_root: Path, excluded_relative_paths: tuple[str, ...] = ()) -> list[str] | None:
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
    return [line for line in status.stdout.splitlines() if line.strip()]


def git_add_all(repo_root: Path, excluded_relative_paths: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    command = ["git", "add", "-A", "--", ".", *[f":(exclude){path}" for path in excluded_relative_paths]]
    return subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def git_repo_root(path: Path) -> Path | None:
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    return Path(probe.stdout.strip())


def build_auto_commit_state(path: Path, run_logger: RunLogger, *, label: str) -> AutoCommitState:
    if shutil.which("git") is None:
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: `git` is not available")
        return AutoCommitState(enabled=False, repo_root=path)
    repo_root = git_repo_root(path)
    if repo_root is None:
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: target directory is not inside a git repository")
        return AutoCommitState(enabled=False, repo_root=path)
    repo_root_resolved = repo_root.resolve(strict=False)
    excluded_relative_paths: tuple[str, ...] = ()
    try:
        log_relative_path = run_logger.log_path.resolve(strict=False).relative_to(repo_root_resolved)
        excluded_relative_paths = (log_relative_path.as_posix(),)
    except ValueError:
        excluded_relative_paths = ()
    has_changes = git_status_has_changes(repo_root, excluded_relative_paths)
    if has_changes is None:
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: failed to inspect git status")
        return AutoCommitState(enabled=False, repo_root=repo_root)
    if has_changes:
        raise AppServerError(
            f"refusing to start: {label} `{repo_root}` has pre-existing changes. "
            "Commit, stash, or discard them before running slop-janitor."
        )
    run_logger.write_line(f"[commit] auto-commit enabled for {label}: {repo_root}")
    return AutoCommitState(
        enabled=True,
        repo_root=repo_root,
        excluded_relative_paths=excluded_relative_paths,
    )


def extract_repo_paths_from_prompt(prompt: str | None) -> list[Path]:
    if not prompt:
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for match in re.findall(r"(?:~|/)[^\s\"']+", prompt):
        raw_path = match.rstrip("`.,:;!?)]}\"'")
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_dir():
            continue
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


def prepare_auto_commit_state(run_cwd: Path, run_logger: RunLogger) -> AutoCommitState:
    return build_auto_commit_state(run_cwd, run_logger, label="primary repo")


def resolve_explicit_linked_repo_roots(linked_repo_paths: list[str]) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    for raw_path in linked_repo_paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.exists():
            raise AppServerError(f"linked repo path does not exist: {candidate}")
        if not candidate.is_dir():
            raise AppServerError(f"linked repo path is not a directory: {candidate}")
        repo_root = git_repo_root(candidate)
        if repo_root is None:
            raise AppServerError(f"linked repo path is not inside a git repository: {candidate}")
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def resolve_prompt_linked_repo_roots(prompt: str | None) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in extract_repo_paths_from_prompt(prompt):
        repo_root = git_repo_root(candidate)
        if repo_root is None:
            continue
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def resolve_linked_repo_roots(*, linked_repo_paths: list[str], prompt: str | None) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    for repo_root in [*resolve_explicit_linked_repo_roots(linked_repo_paths), *resolve_prompt_linked_repo_roots(prompt)]:
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def prepare_auto_commit_states(
    run_cwd: Path,
    prompt: str | None,
    run_logger: RunLogger,
    *,
    linked_repo_paths: list[str] | None = None,
) -> list[AutoCommitState]:
    states = [prepare_auto_commit_state(run_cwd, run_logger)]
    seen_roots = {states[0].repo_root.resolve(strict=False)}
    for repo_root in resolve_linked_repo_roots(linked_repo_paths=linked_repo_paths or [], prompt=prompt):
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)
        states.append(
            build_auto_commit_state(
                repo_root,
                run_logger,
                label=f"linked repo {repo_root}",
            )
        )
    return states


def managed_repo_roots(auto_commits: list[AutoCommitState]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for auto_commit in auto_commits:
        resolved_root = auto_commit.repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        roots.append(auto_commit.repo_root)
    return roots


def sandbox_writable_roots(auto_commits: list[AutoCommitState]) -> list[str]:
    return [str(root.resolve(strict=False)) for root in managed_repo_roots(auto_commits)]


def log_run_scope(
    run_logger: RunLogger,
    *,
    auto_commits: list[AutoCommitState],
    sandbox_mode: str,
) -> None:
    run_logger.write_line(f"sandboxMode={sandbox_mode}")
    for index, repo_root in enumerate(managed_repo_roots(auto_commits), start=1):
        run_logger.write_line(f"managedRepo{index}={repo_root.resolve(strict=False)}")
    if sandbox_mode == "workspace-write":
        for index, writable_root in enumerate(sandbox_writable_roots(auto_commits), start=1):
            run_logger.write_line(f"sandboxWritableRoot{index}={writable_root}")


def validate_sandbox_scope(*, auto_commits: list[AutoCommitState], sandbox_mode: str) -> None:
    if sandbox_mode == "workspace-write" and not sandbox_writable_roots(auto_commits):
        raise AppServerError("workspace-write sandbox requires at least one writable root")


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


def maybe_commit_checkpoints(auto_commits: list[AutoCommitState], run_logger: RunLogger, message: str) -> None:
    for auto_commit in auto_commits:
        maybe_commit_checkpoint(auto_commit, run_logger, message)


def git_has_upstream(repo_root: Path) -> bool:
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return upstream.returncode == 0


def maybe_push_checkpoint(auto_commit: AutoCommitState, run_logger: RunLogger) -> None:
    if not auto_commit.enabled:
        return
    if not git_has_upstream(auto_commit.repo_root):
        run_logger.write_line(f"[push] skipping {auto_commit.repo_root}: no upstream configured")
        return
    push_result = subprocess.run(
        ["git", "push"],
        cwd=auto_commit.repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if push_result.returncode != 0:
        detail = (push_result.stderr or push_result.stdout).strip() or "git push failed"
        run_logger.write_line(f"[push] failed for {auto_commit.repo_root}: {detail}", to_terminal=True, stream="stderr")
        return
    run_logger.write_line(f"[push] pushed {auto_commit.repo_root}")


def maybe_push_checkpoints(auto_commits: list[AutoCommitState], run_logger: RunLogger) -> None:
    for auto_commit in auto_commits:
        maybe_push_checkpoint(auto_commit, run_logger)


def maybe_commit_for_stage(
    auto_commit: AutoCommitState,
    run_logger: RunLogger,
    stage: Stage,
    *,
    mode: str,
    stage_index: int,
    improvement_count: int,
    review_count: int,
) -> None:
    message = checkpoint_message_for_stage(
        stage,
        mode=mode,
        stage_index=stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    if message is not None:
        maybe_commit_checkpoint(auto_commit, run_logger, message)


def maybe_commit_for_stages(
    auto_commits: list[AutoCommitState],
    run_logger: RunLogger,
    stage: Stage,
    *,
    mode: str,
    stage_index: int,
    improvement_count: int,
    review_count: int,
) -> None:
    message = checkpoint_message_for_stage(
        stage,
        mode=mode,
        stage_index=stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    if message is not None:
        maybe_commit_checkpoints(auto_commits, run_logger, message)


def planning_stage_count(mode: str) -> int:
    return 3 if mode == "refactor" else 1


def stages_per_cycle(*, mode: str, improvement_count: int, review_count: int) -> int:
    return planning_stage_count(mode) + improvement_count + review_count + 1


def cycle_number_for_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> int:
    return ((stage_index - 1) // stages_per_cycle(
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def cycle_stage_position(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> int:
    return ((stage_index - 1) % stages_per_cycle(
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def final_planning_stage_position(*, mode: str, improvement_count: int) -> int:
    return planning_stage_count(mode) + improvement_count


def implementation_stage_position(*, mode: str, improvement_count: int) -> int:
    return final_planning_stage_position(mode=mode, improvement_count=improvement_count) + 1


def is_cycle_start_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == 1


def is_final_planning_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == final_planning_stage_position(mode=mode, improvement_count=improvement_count)


def is_implementation_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == implementation_stage_position(mode=mode, improvement_count=improvement_count)


def is_final_review_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    if review_count == 0:
        return False
    return cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == stages_per_cycle(
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )


def is_follow_on_review_stage_index(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    if review_count <= 1:
        return False
    return cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ) > implementation_stage_position(mode=mode, improvement_count=improvement_count) + 1


def checkpoint_message_for_stage(
    stage: Stage,
    *,
    mode: str,
    stage_index: int,
    improvement_count: int,
    review_count: int,
) -> str | None:
    if stage_should_checkpoint(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ):
        return f"slop-janitor: after {stage.label}"
    return None


def stage_should_checkpoint(stage_index: int, *, mode: str, improvement_count: int, review_count: int) -> bool:
    return any(
        (
            is_final_planning_stage_index(
                stage_index,
                mode=mode,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
            is_implementation_stage_index(
                stage_index,
                mode=mode,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
            is_final_review_stage_index(
                stage_index,
                mode=mode,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
        )
    )


def stage_should_start_clean(*, mode: str, stage_index: int, improvement_count: int, review_count: int) -> bool:
    return not is_follow_on_review_stage_index(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )


def terminal_phase_label(
    *,
    mode: str,
    stage: Stage,
    stage_index: int,
    improvement_count: int,
    review_count: int,
) -> str:
    position = cycle_stage_position(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    if stage.skill_name == "find-refactor-candidates":
        return "Refactor Discovery"
    if stage.skill_name == "select-refactor":
        return "Refactor Selection"
    if stage.skill_name == "execplan-create":
        return "ExecPlan Planning"
    if stage.skill_name in IMPROVE_SKILL_CHOICES:
        improve_index = position - planning_stage_count(mode)
        return f"Improvement Pass {improve_index}/{improvement_count}"
    if stage.skill_name == "implement-execplan":
        return "Implementation"
    review_index = position - implementation_stage_position(mode=mode, improvement_count=improvement_count)
    return f"Review Pass {review_index}/{review_count}"


def write_terminal_stage_heading(
    run_logger: RunLogger,
    *,
    mode: str,
    stage: Stage,
    stage_index: int,
    total_stages: int,
    cycles: int,
    improvement_count: int,
    review_count: int,
) -> None:
    cycle_number = cycle_number_for_stage_index(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    phase_label = terminal_phase_label(
        mode=mode,
        stage=stage,
        stage_index=stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )

    run_logger.write_line("")
    if is_cycle_start_stage_index(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ):
        run_logger.write_line(
            f"========== Workflow Cycle {cycle_number}/{cycles} ==========",
            to_terminal=True,
        )
    run_logger.write_line(f"--- {phase_label} ---", to_terminal=True)
    run_logger.write_line(f"Stage {stage_index}/{total_stages} · {stage.label}", to_terminal=True)
    run_logger.write_line("")


def pending_execplan_path(run_cwd: Path) -> Path:
    return run_cwd / ".agent" / "execplan-pending.md"


def agent_dir(run_cwd: Path) -> Path:
    return run_cwd / ".agent"


def active_work_item_link_path(run_cwd: Path) -> Path:
    return agent_dir(run_cwd) / "active"


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_active_work_item_dir(run_cwd: Path) -> Path | None:
    active_link = active_work_item_link_path(run_cwd)
    if not active_link.exists() and not active_link.is_symlink():
        return None
    resolved = active_link.resolve(strict=False)
    if resolved.exists() and resolved.is_dir():
        return resolved
    if active_link.is_dir():
        return active_link.resolve(strict=False)
    return None


def work_item_artifact_path(work_item_dir: Path, artifact_key: str, default_name: str) -> Path:
    meta = read_json_object(work_item_dir / "meta.json") or {}
    artifacts = meta.get("artifacts")
    if isinstance(artifacts, dict):
        value = artifacts.get(artifact_key)
        if isinstance(value, str) and value:
            return work_item_dir / value
    return work_item_dir / default_name


def active_work_item_artifact_path(run_cwd: Path, artifact_key: str, default_name: str) -> Path | None:
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is None:
        return None
    return work_item_artifact_path(work_item_dir, artifact_key, default_name)


def workflow_tracking_paths(run_cwd: Path) -> tuple[Path, ...]:
    paths: list[Path] = [active_work_item_link_path(run_cwd), pending_execplan_path(run_cwd)]
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is not None:
        paths.extend(
            [
                work_item_dir / "meta.json",
                work_item_artifact_path(work_item_dir, "candidates", "candidates.md"),
                work_item_artifact_path(work_item_dir, "decision", "decision.md"),
                work_item_artifact_path(work_item_dir, "execplan", "execplan.md"),
            ]
        )
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return tuple(deduped)


def relative_path_from_repo(repo_root: Path, path: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(repo_root.resolve(strict=False)).as_posix()
    except ValueError:
        return None


def combine_excluded_relative_paths(*groups: tuple[str, ...]) -> tuple[str, ...]:
    combined: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path in seen:
                continue
            seen.add(path)
            combined.append(path)
    return tuple(combined)


def allowed_dirty_paths_for_stage(
    repo_root: Path,
    run_cwd: Path,
    stage: Stage,
    *,
    phase: str,
) -> tuple[str, ...]:
    primary_repo_root = git_repo_root(run_cwd)
    if primary_repo_root is None:
        return ()
    if repo_root.resolve(strict=False) != primary_repo_root.resolve(strict=False):
        return ()
    tracked_relative_paths = tuple(
        relative_path
        for relative_path in (
            relative_path_from_repo(repo_root, path)
            for path in workflow_tracking_paths(run_cwd)
        )
        if relative_path is not None
    )
    tracked_relative_paths = combine_excluded_relative_paths(tracked_relative_paths, (".agent",))
    if phase == "start" and stage.skill_name in {
        "select-refactor",
        "execplan-create",
        *IMPROVE_SKILL_CHOICES,
        "implement-execplan",
    }:
        return tracked_relative_paths
    if phase == "end" and stage.skill_name in {
        "find-refactor-candidates",
        "select-refactor",
        "execplan-create",
        *IMPROVE_SKILL_CHOICES,
    }:
        return tracked_relative_paths
    return ()


def ensure_auto_commit_workspaces_clean(
    auto_commits: list[AutoCommitState],
    run_cwd: Path,
    stage: Stage,
    *,
    phase: str,
) -> None:
    for auto_commit in auto_commits:
        if not auto_commit.enabled:
            continue
        excluded_relative_paths = combine_excluded_relative_paths(
            auto_commit.excluded_relative_paths,
            allowed_dirty_paths_for_stage(auto_commit.repo_root, run_cwd, stage, phase=phase),
        )
        status_lines = git_status_lines(auto_commit.repo_root, excluded_relative_paths)
        if status_lines is None:
            raise AppServerError(
                f"stage `{stage.label}` could not inspect git status for auto-managed repo `{auto_commit.repo_root}`"
            )
        if not status_lines:
            continue
        phase_text = "before starting" if phase == "start" else "after completing"
        detail = "; ".join(status_lines[:5])
        if len(status_lines) > 5:
            detail = f"{detail}; ..."
        raise AppServerError(
            f"stage `{stage.label}` {phase_text}: auto-managed repo `{auto_commit.repo_root}` "
            f"has local changes outside allowed stage artifacts: {detail}"
        )


def stage_should_end_clean(*, mode: str, stage_index: int, improvement_count: int, review_count: int) -> bool:
    return stage_should_checkpoint(
        mode=mode,
        stage_index=stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )


def read_execplan_snapshot(path: Path) -> ExecPlanSnapshot | None:
    if not path.is_file():
        return None
    stat = path.stat()
    return ExecPlanSnapshot(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def preferred_execplan_path(run_cwd: Path) -> Path:
    active_execplan = active_work_item_artifact_path(run_cwd, "execplan", "execplan.md")
    if active_execplan is not None:
        return active_execplan
    return pending_execplan_path(run_cwd)


def ensure_execplan_exists(run_cwd: Path, stage: Stage) -> None:
    path = preferred_execplan_path(run_cwd)
    if path.is_file():
        return
    raise AppServerError(
        f"stage `{stage.label}` requires an execplan, but `{path}` is missing"
    )


def stage_primary_artifact_path(run_cwd: Path, stage: Stage) -> Path | None:
    if stage.skill_name == "find-refactor-candidates":
        return active_work_item_artifact_path(run_cwd, "candidates", "candidates.md")
    if stage.skill_name == "select-refactor":
        return active_work_item_artifact_path(run_cwd, "decision", "decision.md")
    if stage.skill_name in {"execplan-create", *IMPROVE_SKILL_CHOICES}:
        return preferred_execplan_path(run_cwd)
    if stage.skill_name == "implement-execplan":
        work_item_dir = resolve_active_work_item_dir(run_cwd)
        if work_item_dir is not None:
            return work_item_dir / "meta.json"
        return pending_execplan_path(run_cwd)
    return None


def ensure_cycle_start_artifact_was_refreshed(
    run_cwd: Path,
    stage: Stage,
    *,
    previous_snapshot: WorkflowArtifactSnapshot,
) -> None:
    current_snapshot = stage_primary_artifact_snapshot(run_cwd, stage)
    if current_snapshot.path is None or not current_snapshot.fingerprint.exists:
        missing_path = current_snapshot.path or "<unknown artifact>"
        raise AppServerError(
            f"stage `{stage.label}` did not produce `{missing_path}`"
        )
    if current_snapshot == previous_snapshot:
        raise AppServerError(
            f"stage `{stage.label}` did not refresh `{current_snapshot.path}` for the new cycle"
        )


def implementation_state_completed(run_cwd: Path) -> bool:
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is not None:
        meta = read_json_object(work_item_dir / "meta.json") or {}
        return meta.get("stage") == "implementation" and meta.get("state") == "completed"
    return not pending_execplan_path(run_cwd).exists()


def ensure_implementation_completed(run_cwd: Path, stage: Stage) -> None:
    if implementation_state_completed(run_cwd):
        return
    path = stage_primary_artifact_path(run_cwd, stage) or preferred_execplan_path(run_cwd)
    raise AppServerError(
        f"stage `{stage.label}` completed but did not mark implementation as completed: `{path}`"
    )


def git_head_commit(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def fingerprint_path(path: Path) -> FileFingerprint:
    if path.is_symlink():
        target = os.readlink(path)
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return FileFingerprint(exists=True, size=len(target), sha256=digest)
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                digest.update(chunk)
        return FileFingerprint(exists=True, size=path.stat().st_size, sha256=digest.hexdigest())
    if path.exists():
        return FileFingerprint(exists=True, size=0, sha256="dir")
    return FileFingerprint(exists=False, size=0, sha256=None)


def stage_primary_artifact_snapshot(run_cwd: Path, stage: Stage) -> WorkflowArtifactSnapshot:
    path = stage_primary_artifact_path(run_cwd, stage)
    return WorkflowArtifactSnapshot(
        path=str(path) if path is not None else None,
        fingerprint=fingerprint_path(path) if path is not None else FileFingerprint(exists=False, size=0, sha256=None),
    )


def capture_stage_workspace_snapshot(
    auto_commits: list[AutoCommitState],
    run_cwd: Path,
    stage: Stage,
) -> StageWorkspaceSnapshot:
    repo_states: list[RepoStateSnapshot] = []
    for auto_commit in auto_commits:
        if not auto_commit.enabled:
            continue
        excluded_relative_paths = combine_excluded_relative_paths(
            auto_commit.excluded_relative_paths,
            allowed_dirty_paths_for_stage(auto_commit.repo_root, run_cwd, stage, phase="start"),
        )
        status_lines = git_status_lines(auto_commit.repo_root, excluded_relative_paths)
        if status_lines is None:
            raise AppServerError(
                f"stage `{stage.label}` could not inspect git status for auto-managed repo `{auto_commit.repo_root}`"
            )
        repo_states.append(
            RepoStateSnapshot(
                repo_root=auto_commit.repo_root,
                head_commit=git_head_commit(auto_commit.repo_root),
                status_lines=tuple(status_lines),
            )
        )
    return StageWorkspaceSnapshot(
        repo_states=tuple(repo_states),
        tracked_artifacts=tuple(
            WorkflowArtifactSnapshot(path=str(path), fingerprint=fingerprint_path(path))
            for path in workflow_tracking_paths(run_cwd)
        ),
    )


def stage_workspace_matches(
    snapshot: StageWorkspaceSnapshot,
    *,
    auto_commits: list[AutoCommitState],
    run_cwd: Path,
    stage: Stage,
) -> bool:
    current = capture_stage_workspace_snapshot(auto_commits, run_cwd, stage)
    return current == snapshot


def serialize_workspace_snapshot(snapshot: StageWorkspaceSnapshot) -> dict[str, Any]:
    return {
        "trackedArtifacts": [
            {
                "path": artifact.path,
                "exists": artifact.fingerprint.exists,
                "size": artifact.fingerprint.size,
                "sha256": artifact.fingerprint.sha256,
            }
            for artifact in snapshot.tracked_artifacts
        ],
        "repos": [
            {
                "repoRoot": str(repo_state.repo_root),
                "headCommit": repo_state.head_commit,
                "statusLines": list(repo_state.status_lines),
            }
            for repo_state in snapshot.repo_states
        ],
    }


def stage_postconditions_satisfied(
    *,
    run_cwd: Path,
    stage: Stage,
    mode: str,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    cycle_start_artifact_snapshot: WorkflowArtifactSnapshot,
) -> tuple[bool, str | None]:
    if is_cycle_start_stage_index(
        stage_index,
        mode=mode,
        improvement_count=improvement_count,
        review_count=review_count,
    ):
        current_snapshot = stage_primary_artifact_snapshot(run_cwd, stage)
        if current_snapshot != cycle_start_artifact_snapshot and current_snapshot.fingerprint.exists:
            return True, "cycle-start artifact was refreshed despite the transient failure"
    if stage.skill_name == "implement-execplan" and implementation_state_completed(run_cwd):
        return True, "implementation state completed despite the transient failure"
    return False, None


def retryable_error_text(*texts: str | None) -> bool:
    haystack = " ".join(text.lower() for text in texts if text).strip()
    if not haystack:
        return False
    return any(
        phrase in haystack
        for phrase in (
            "selected model is at capacity",
            "serveroverloaded",
            "temporarily unavailable",
            "temporarily overloaded",
            "model is overloaded",
            "try a different model",
        )
    )


def failure_assessment_from_turn_error(error_message: str | None, error_payload: dict[str, Any] | None) -> FailureAssessment:
    if retryable_error_text(error_message, json.dumps(error_payload, sort_keys=True) if error_payload else None):
        return FailureAssessment(retryable=True, restart_client=False, reason="transient model capacity failure")
    return FailureAssessment(retryable=False, restart_client=False, reason="terminal stage failure")


def failure_assessment_from_exception(exc: AppServerError) -> FailureAssessment:
    if isinstance(exc, AppServerTimeoutError):
        return FailureAssessment(retryable=True, restart_client=True, reason="stage stopped producing app-server activity")
    if isinstance(exc, AppServerRequestError) and exc.method == "turn/start" and retryable_error_text(exc.message):
        return FailureAssessment(retryable=True, restart_client=False, reason="transient turn/start rejection")
    if "stdout closed unexpectedly" in str(exc).lower():
        return FailureAssessment(retryable=True, restart_client=True, reason="app-server process died mid-stage")
    return FailureAssessment(retryable=False, restart_client=False, reason="terminal app-server failure")


def start_client_and_thread(
    *,
    client_spawn_spec: AppServerSpawnSpec,
    run_logger: RunLogger,
    run_cwd: Path,
    sandbox_mode: str,
    writable_roots: list[str],
    request_timeout_seconds: float,
) -> tuple[AppServerClient, str]:
    client = AppServerClient(client_spawn_spec, run_logger)
    try:
        client.start()
        client.initialize(request_timeout_seconds=request_timeout_seconds)
        account_info = client.get_account(request_timeout_seconds=request_timeout_seconds)
        if account_info.get("requiresOpenaiAuth") and account_info.get("account") is None:
            raise AppServerError(
                "OpenAI auth is required before starting the pipeline. Run `./slop-janitor auth login`."
            )
        return client, client.start_thread(
            str(run_cwd),
            sandbox_mode=sandbox_mode,
            writable_roots=writable_roots,
            request_timeout_seconds=request_timeout_seconds,
        )
    except AppServerError:
        client.close()
        raise


def execute_stage_with_recovery(
    *,
    client: AppServerClient,
    thread_id: str,
    client_spawn_spec: AppServerSpawnSpec,
    run_logger: RunLogger,
    run_state: RunStateTracker,
    run_cwd: Path,
    auto_commits: list[AutoCommitState],
    stage: Stage,
    mode: str,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    sandbox_mode: str,
    writable_roots: list[str],
    cycle_start_artifact_snapshot: WorkflowArtifactSnapshot,
    stage_idle_timeout_seconds: float,
    max_stage_retries: int,
    retry_initial_delay_seconds: float,
    retry_max_delay_seconds: float,
) -> StageExecutionOutcome:
    stage_snapshot = capture_stage_workspace_snapshot(auto_commits, run_cwd, stage)
    delay_seconds = retry_initial_delay_seconds
    attempt = 1
    while True:
        run_state.update(
            status="running",
            currentStage={
                "index": stage_index,
                "label": stage.label,
                "skillName": stage.skill_name,
                "attempt": attempt,
                "threadId": thread_id,
                "workspaceSnapshot": serialize_workspace_snapshot(stage_snapshot),
            },
        )
        try:
            result = client.run_turn(
                thread_id,
                stage,
                idle_timeout_seconds=stage_idle_timeout_seconds,
                request_timeout_seconds=stage_idle_timeout_seconds,
            )
        except AppServerError as exc:
            failure_message = str(exc)
            assessment = failure_assessment_from_exception(exc)
            token_usage = None
        else:
            token_usage = result.token_usage
            if result.status == "completed":
                return StageExecutionOutcome(client=client, thread_id=thread_id, token_usage=token_usage)
            failure_message = result.error_message or "unknown turn failure"
            assessment = failure_assessment_from_turn_error(result.error_message, result.error_payload)

        if not assessment.retryable:
            raise AppServerError(f"Stage {stage.label} failed: {failure_message}")
        postconditions_satisfied, postcondition_reason = stage_postconditions_satisfied(
            run_cwd=run_cwd,
            stage=stage,
            mode=mode,
            stage_index=stage_index,
            improvement_count=improvement_count,
            review_count=review_count,
            cycle_start_artifact_snapshot=cycle_start_artifact_snapshot,
        )
        if postconditions_satisfied:
            if token_usage is None:
                raise AppServerError(
                    f"stage `{stage.label}` satisfied recovery postconditions but did not report token usage"
                )
            run_logger.write_line(
                f"[retry] continuing after transient failure in stage `{stage.label}`: {postcondition_reason}",
                to_terminal=True,
            )
            return StageExecutionOutcome(
                client=client,
                thread_id=thread_id,
                token_usage=token_usage,
                recovered_via_postconditions=True,
            )
        if not stage_workspace_matches(stage_snapshot, auto_commits=auto_commits, run_cwd=run_cwd, stage=stage):
            raise AppServerError(
                f"stage `{stage.label}` hit a retryable failure but left workspace changes that make replay unsafe: "
                f"{failure_message}"
            )
        if attempt > max_stage_retries:
            raise AppServerError(
                f"stage `{stage.label}` exhausted {max_stage_retries} retry attempt(s): {failure_message}"
            )
        if assessment.restart_client:
            run_logger.write_line(
                f"[retry] restarting Codex app-server for stage `{stage.label}` after {assessment.reason}: "
                f"{failure_message}",
                to_terminal=True,
                stream="stderr",
            )
            client.close()
            client, thread_id = start_client_and_thread(
                client_spawn_spec=client_spawn_spec,
                run_logger=run_logger,
                run_cwd=run_cwd,
                sandbox_mode=sandbox_mode,
                writable_roots=writable_roots,
                request_timeout_seconds=stage_idle_timeout_seconds,
            )
        else:
            run_logger.write_line(
                f"[retry] retrying stage `{stage.label}` after {assessment.reason}: {failure_message}",
                to_terminal=True,
                stream="stderr",
            )
        run_logger.write_line(
            f"[retry] waiting {delay_seconds:.1f} second(s) before attempt {attempt + 1}",
            to_terminal=True,
        )
        run_state.update(
            status="retrying",
            currentStage={
                "index": stage_index,
                "label": stage.label,
                "skillName": stage.skill_name,
                "attempt": attempt,
                "threadId": thread_id,
                "lastError": failure_message,
                "recoveryReason": assessment.reason,
                "nextDelaySeconds": delay_seconds,
                "workspaceSnapshot": serialize_workspace_snapshot(stage_snapshot),
            },
        )
        time.sleep(delay_seconds)
        delay_seconds = min(delay_seconds * 2, retry_max_delay_seconds)
        attempt += 1


def maybe_delay_between_cycles(
    *,
    mode: str,
    stage_index: int,
    total_stages: int,
    improvement_count: int,
    review_count: int,
    delay_between_cycles_minutes: float,
    run_logger: RunLogger,
) -> None:
    if delay_between_cycles_minutes <= 0:
        return
    if stage_index >= total_stages:
        return
    if stage_index % stages_per_cycle(mode=mode, improvement_count=improvement_count, review_count=review_count) != 0:
        return
    run_logger.write_line(
        f"Sleeping {delay_between_cycles_minutes} minute(s) before the next cycle.",
        to_terminal=True,
    )
    time.sleep(delay_between_cycles_minutes * 60)


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
    run_state: RunStateTracker | None = None
    auto_commits: list[AutoCommitState] = []
    try:
        run_cwd = Path.cwd()
        run_logger = create_run_logger(
            runs_dir=runs_dir or DEFAULT_RUNS_DIR,
            run_cwd=run_cwd,
            mode=args.mode,
            prompt=args.prompt,
        )
        run_state = RunStateTracker(
            run_logger.log_path.with_suffix(".state.json"),
            run_cwd=run_cwd,
            mode=args.mode,
            prompt=args.prompt,
        )
        run_logger.write_line(f"cycles={args.cycles}")
        run_logger.write_line(f"improvements={args.improvements}")
        run_logger.write_line(f"improveSkill={args.improve_skill}")
        run_logger.write_line(f"review={args.review}")
        run_logger.write_line(f"reviewSkill={args.review_skill}")
        run_logger.write_line(f"linkedRepos={json.dumps(args.linked_repo)}")
        run_logger.write_line(f"delayBetweenCyclesMinutes={args.delay_between_cycles_minutes}")
        run_logger.write_line(f"stageIdleTimeoutSeconds={args.stage_idle_timeout_seconds}")
        run_logger.write_line(f"maxStageRetries={args.max_stage_retries}")
        run_logger.write_line(f"retryInitialDelaySeconds={args.retry_initial_delay_seconds}")
        run_logger.write_line(f"retryMaxDelaySeconds={args.retry_max_delay_seconds}")
        run_logger.write_line("")
        validate_counts(
            cycles=args.cycles,
            improvement_count=args.improvements,
            review_count=args.review,
            delay_between_cycles_minutes=args.delay_between_cycles_minutes,
            stage_idle_timeout_seconds=args.stage_idle_timeout_seconds,
            max_stage_retries=args.max_stage_retries,
            retry_initial_delay_seconds=args.retry_initial_delay_seconds,
            retry_max_delay_seconds=args.retry_max_delay_seconds,
        )
        stages = build_stages(
            args.mode,
            args.prompt,
            cycles=args.cycles,
            improvement_count=args.improvements,
            review_count=args.review,
            improve_skill_name=args.improve_skill,
            review_skill_name=args.review_skill,
        )
        validate_skills(stages)

        if spawn_spec is None:
            codex_workspace = resolve_codex_workspace(args.codex_workspace)
            validate_workspace(codex_workspace)
            validate_cargo()
            client_spawn_spec = default_app_server_spawn_spec(codex_workspace)
        else:
            client_spawn_spec = spawn_spec

        auto_commits = prepare_auto_commit_states(
            run_cwd,
            args.prompt,
            run_logger,
            linked_repo_paths=args.linked_repo,
        )
        validate_sandbox_scope(auto_commits=auto_commits, sandbox_mode=args.sandbox)
        log_run_scope(run_logger, auto_commits=auto_commits, sandbox_mode=args.sandbox)
        run_logger.write_line("")

        thread_id: str | None = None
        cycle_start_artifact_snapshot = WorkflowArtifactSnapshot(
            path=None,
            fingerprint=FileFingerprint(exists=False, size=0, sha256=None),
        )
        writable_roots = sandbox_writable_roots(auto_commits)
        for index, stage in enumerate(stages, start=1):
            if is_cycle_start_stage_index(
                index,
                mode=args.mode,
                improvement_count=args.improvements,
                review_count=args.review,
            ):
                if client is None:
                    client, thread_id = start_client_and_thread(
                        client_spawn_spec=client_spawn_spec,
                        run_logger=run_logger,
                        run_cwd=run_cwd,
                        sandbox_mode=args.sandbox,
                        writable_roots=writable_roots,
                        request_timeout_seconds=args.stage_idle_timeout_seconds,
                    )
                    run_logger.write_line("Codex app-server ready.", to_terminal=True)
                    run_logger.write_line("", to_terminal=True)
                else:
                    thread_id = client.start_thread(
                        str(run_cwd),
                        sandbox_mode=args.sandbox,
                        writable_roots=writable_roots,
                        request_timeout_seconds=args.stage_idle_timeout_seconds,
                    )
                run_state.update(status="ready", currentThreadId=thread_id, currentCycle=cycle_number_for_stage_index(
                    index,
                    mode=args.mode,
                    improvement_count=args.improvements,
                    review_count=args.review,
                ))
                cycle_start_artifact_snapshot = stage_primary_artifact_snapshot(run_cwd, stage)
            if thread_id is None:
                raise AppServerError("failed to start a cycle thread")
            if stage_should_start_clean(
                mode=args.mode,
                stage_index=index,
                improvement_count=args.improvements,
                review_count=args.review,
            ):
                ensure_auto_commit_workspaces_clean(auto_commits, run_cwd, stage, phase="start")
            if stage.skill_name in {*IMPROVE_SKILL_CHOICES, "implement-execplan"}:
                ensure_execplan_exists(run_cwd, stage)
            write_terminal_stage_heading(
                run_logger,
                mode=args.mode,
                stage=stage,
                stage_index=index,
                total_stages=len(stages),
                cycles=args.cycles,
                improvement_count=args.improvements,
                review_count=args.review,
            )
            run_logger.write_line(f"=== Stage {index}/{len(stages)}: {stage.label} ===")
            outcome = execute_stage_with_recovery(
                client=client,
                thread_id=thread_id,
                client_spawn_spec=client_spawn_spec,
                run_logger=run_logger,
                run_state=run_state,
                run_cwd=run_cwd,
                auto_commits=auto_commits,
                stage=stage,
                mode=args.mode,
                stage_index=index,
                improvement_count=args.improvements,
                review_count=args.review,
                sandbox_mode=args.sandbox,
                writable_roots=writable_roots,
                cycle_start_artifact_snapshot=cycle_start_artifact_snapshot,
                stage_idle_timeout_seconds=args.stage_idle_timeout_seconds,
                max_stage_retries=args.max_stage_retries,
                retry_initial_delay_seconds=args.retry_initial_delay_seconds,
                retry_max_delay_seconds=args.retry_max_delay_seconds,
            )
            client = outcome.client
            thread_id = outcome.thread_id
            if outcome.token_usage is not None:
                write_token_footer(run_logger, outcome.token_usage)
            if is_cycle_start_stage_index(
                index,
                mode=args.mode,
                improvement_count=args.improvements,
                review_count=args.review,
            ):
                ensure_cycle_start_artifact_was_refreshed(
                    run_cwd,
                    stage,
                    previous_snapshot=cycle_start_artifact_snapshot,
                )
            if stage.skill_name == "implement-execplan":
                ensure_implementation_completed(run_cwd, stage)
            maybe_commit_for_stages(
                auto_commits,
                run_logger,
                stage,
                mode=args.mode,
                stage_index=index,
                improvement_count=args.improvements,
                review_count=args.review,
            )
            if stage_should_end_clean(
                mode=args.mode,
                stage_index=index,
                improvement_count=args.improvements,
                review_count=args.review,
            ):
                ensure_auto_commit_workspaces_clean(auto_commits, run_cwd, stage, phase="end")
            maybe_delay_between_cycles(
                mode=args.mode,
                stage_index=index,
                total_stages=len(stages),
                improvement_count=args.improvements,
                review_count=args.review,
                delay_between_cycles_minutes=args.delay_between_cycles_minutes,
                run_logger=run_logger,
            )
        maybe_commit_checkpoints(auto_commits, run_logger, "slop-janitor: final checkpoint")
        maybe_push_checkpoints(auto_commits, run_logger)
        run_state.close(status="completed")
        return 0
    except AppServerError as exc:
        if run_state is not None:
            run_state.close(status="failed")
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

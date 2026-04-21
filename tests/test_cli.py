from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from slop_janitor.app_server import AppServerError
from slop_janitor.app_server import AppServerSpawnSpec
from slop_janitor.cli import build_refactor_stages
from slop_janitor.cli import build_stages
from slop_janitor.cli import ensure_auto_commit_workspaces_clean
from slop_janitor.cli import main
from slop_janitor.cli import maybe_commit_checkpoints
from slop_janitor.cli import maybe_commit_checkpoint
from slop_janitor.cli import maybe_commit_for_stages
from slop_janitor.cli import maybe_commit_for_stage
from slop_janitor.cli import maybe_push_checkpoints
from slop_janitor.cli import prepare_auto_commit_states
from slop_janitor.cli import prepare_auto_commit_state
from slop_janitor.cli import resolve_codex_workspace
from slop_janitor.cli import run
from slop_janitor.cli import run_auth
from slop_janitor.run_log import RunLogger


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_app_server.py"
FAKE_CODEX_CLI = REPO_ROOT / "tests" / "fixtures" / "fake_codex_cli.py"
PROMPT = "help me build a CRM"
DEFAULT_REFACTOR_PROMPT = "identify the top materially different refactor candidates in this repository"


@contextlib.contextmanager
def chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class CliTests(unittest.TestCase):
    maxDiff = None

    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
        (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)

    def init_git_remote(self, repo_root: Path) -> Path:
        remote_root = repo_root.parent / f"{repo_root.name}-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote_root)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote_root)], cwd=repo_root, check=True)
        subprocess.run(["git", "push", "-u", "origin", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True)
        return remote_root

    def assert_remote_head_matches_local(self, repo_root: Path, remote_root: Path) -> None:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        local_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        remote_head = subprocess.run(
            ["git", f"--git-dir={remote_root}", "rev-parse", f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, local_head)

    def make_app_server_spawn_spec(
        self,
        scenario: str,
        record_path: Path,
        *,
        config_path: Path | None = None,
        cwd: Path | None = None,
    ) -> AppServerSpawnSpec:
        return AppServerSpawnSpec(
            argv=tuple(
                [
                    sys.executable,
                    str(FAKE_APP_SERVER),
                    scenario,
                    str(record_path),
                    *([str(config_path)] if config_path is not None else []),
                ]
            ),
            cwd=str(cwd or REPO_ROOT),
        )

    def make_codex_cli_spawn_spec(self, record_path: Path, *, cwd: Path | None = None) -> AppServerSpawnSpec:
        return AppServerSpawnSpec(
            argv=(sys.executable, str(FAKE_CODEX_CLI), str(record_path)),
            cwd=str(cwd or REPO_ROOT),
        )

    def run_workflow(
        self,
        scenario: str,
        *,
        argv: list[str] | None = None,
        target_cwd: Path | None = None,
        server_cwd: Path | None = None,
    ) -> tuple[int, str, str, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        record_path = Path(tempdir.name) / f"{scenario}.json"
        config_path = Path(tempdir.name) / f"{scenario}-config.json"
        runs_dir = Path(tempdir.name) / "runs"
        default_target_cwd = Path(tempdir.name) / "workspace"
        default_target_cwd.mkdir()
        stdout = io.StringIO()
        stderr = io.StringIO()
        cli_argv = argv or []
        prompt: str | None = PROMPT
        cycles = 1
        improvements = 1
        review = 1
        improve_skill = "execplan-improve"
        review_skill = "review-recent-work"
        index = 0
        while index < len(cli_argv):
            token = cli_argv[index]
            if token == "--prompt":
                prompt = cli_argv[index + 1]
                index += 2
                continue
            if token == "--cycles":
                cycles = int(cli_argv[index + 1])
                index += 2
                continue
            if token == "--improvements":
                improvements = int(cli_argv[index + 1])
                index += 2
                continue
            if token == "--improve-skill":
                improve_skill = cli_argv[index + 1]
                index += 2
                continue
            if token == "--review":
                review = int(cli_argv[index + 1])
                index += 2
                continue
            if token == "--review-skill":
                review_skill = cli_argv[index + 1]
                index += 2
                continue
            index += 1
        if "--prompt" not in cli_argv:
            prompt = None
        config_path.write_text(
            json.dumps(
                {
                    "prompt": prompt,
                    "cycles": cycles,
                    "improvements": improvements,
                    "improve_skill": improve_skill,
                    "review": review,
                    "review_skill": review_skill,
                }
            ),
            encoding="utf-8",
        )
        with chdir(target_cwd or default_target_cwd):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = run(
                    cli_argv,
                    spawn_spec=self.make_app_server_spawn_spec(
                        scenario,
                        record_path,
                        config_path=config_path,
                        cwd=server_cwd,
                    ),
                    runs_dir=runs_dir,
                )
        return exit_code, stdout.getvalue(), stderr.getvalue(), record_path

    def run_pipeline(
        self,
        scenario: str,
        *,
        argv: list[str] | None = None,
        target_cwd: Path | None = None,
        server_cwd: Path | None = None,
    ) -> tuple[int, str, str, Path]:
        return self.run_workflow(
            scenario,
            argv=argv,
            target_cwd=target_cwd,
            server_cwd=server_cwd,
        )

    def run_auth_command(self, argv: list[str]) -> tuple[int, str, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        record_path = Path(tempdir.name) / "auth.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = run_auth(argv, codex_cli_spawn_spec=self.make_codex_cli_spawn_spec(record_path))
        return exit_code, stdout.getvalue(), record_path

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def inbound_messages(self, record: dict) -> list[dict]:
        return [entry["message"] for entry in record["transcript"] if entry["direction"] == "in"]

    def read_run_log(self, record_path: Path) -> tuple[Path, str]:
        log_paths = sorted((record_path.parent / "runs").glob("*.log"))
        self.assertEqual(len(log_paths), 1)
        return log_paths[0], log_paths[0].read_text(encoding="utf-8")

    def test_auth_login_delegates_to_codex_cli(self) -> None:
        exit_code, _, record_path = self.run_auth_command(["login"])
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(record["argv"], ["login"])

    def test_auth_login_forwards_device_auth_flag(self) -> None:
        exit_code, _, record_path = self.run_auth_command(["login", "--device-auth"])
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(record["argv"], ["login", "--device-auth"])

    def test_auth_wrapper_forwards_config_overrides(self) -> None:
        exit_code, _, record_path = self.run_auth_command(
            ["login", "--device-auth", "--config", "forced_login_method=chatgpt"]
        )
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            record["argv"],
            ["--config", "forced_login_method=chatgpt", "login", "--device-auth"],
        )

    def test_auth_wrapper_forwards_equals_config_overrides(self) -> None:
        exit_code, _, record_path = self.run_auth_command(
            ["login", "--config=forced_login_method=chatgpt", "--device-auth"]
        )
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            record["argv"],
            ["--config=forced_login_method=chatgpt", "login", "--device-auth"],
        )

    def test_auth_status_delegates_to_codex_cli(self) -> None:
        exit_code, _, record_path = self.run_auth_command(["status"])
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(record["argv"], ["login", "status"])

    def test_missing_openai_auth_fails_with_auth_hint(self) -> None:
        target_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(target_dir, ignore_errors=True))
        exit_code, _, stderr, record_path = self.run_workflow("missing_auth", target_cwd=target_dir)
        record = self.read_json(record_path)
        methods = [message["method"] for message in self.inbound_messages(record) if "method" in message]

        self.assertEqual(exit_code, 1)
        self.assertIn("slop-janitor auth login", stderr)
        self.assertNotIn("thread/start", methods)

    def test_default_workflow_uses_refactor_candidate_prompt_when_missing(self) -> None:
        exit_code, _, stderr, record_path = self.run_workflow("refactor_without_prompt", argv=[])
        record = self.read_json(record_path)
        turn_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            turn_start["params"]["input"][0]["text"],
            f"$find-refactor-candidates {DEFAULT_REFACTOR_PROMPT}",
        )

    def test_invalid_cycles_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run(["--prompt", PROMPT, "--cycles", "0"], runs_dir=Path(tempdir.name) / "runs")

        self.assertEqual(exit_code, 1)
        self.assertIn("`--cycles` must be at least 1", stderr.getvalue())

    def test_invalid_improvements_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run(
                ["--prompt", PROMPT, "--improvements", "-1"],
                runs_dir=Path(tempdir.name) / "runs",
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("`--improvements` must be 0 or greater", stderr.getvalue())

    def test_invalid_review_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run(["--prompt", PROMPT, "--review", "-1"], runs_dir=Path(tempdir.name) / "runs")

        self.assertEqual(exit_code, 1)
        self.assertIn("`--review` must be 0 or greater", stderr.getvalue())

    def test_invalid_delay_between_cycles_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run(
                ["--prompt", PROMPT, "--delay-between-cycles-minutes", "-1"],
                runs_dir=Path(tempdir.name) / "runs",
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("`--delay-between-cycles-minutes` must be 0 or greater", stderr.getvalue())

    def test_invalid_runs_dir_fails_cleanly(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        runs_file = Path(tempdir.name) / "runs"
        runs_file.write_text("not a directory", encoding="utf-8")
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = run(["--prompt", PROMPT], runs_dir=runs_file)

        self.assertEqual(exit_code, 1)
        self.assertIn("failed to create run log at", stderr.getvalue())

    def test_codex_workspace_cli_flag_overrides_env(self) -> None:
        cli_path = Path("/tmp/codex-cli")
        env_path = Path("/tmp/codex-env")
        with mock.patch.dict(os.environ, {"CODEX_WORKSPACE": str(env_path)}, clear=False):
            resolved = resolve_codex_workspace(str(cli_path))

        self.assertEqual(resolved, cli_path)

    def test_missing_codex_workspace_fails_cleanly(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stderr(stderr):
                exit_code = run(["--prompt", PROMPT], runs_dir=Path(tempdir.name) / "runs")

        self.assertEqual(exit_code, 1)
        self.assertIn("Codex workspace is not configured", stderr.getvalue())
        self.assertIn("CODEX_WORKSPACE", stderr.getvalue())

    def test_invalid_auth_command_fails_before_workspace_lookup(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stderr(stderr):
                exit_code = main(["auth", "statuz"])

        self.assertEqual(exit_code, 1)
        self.assertIn("unsupported auth command: statuz", stderr.getvalue())
        self.assertNotIn("Codex workspace is not configured", stderr.getvalue())

    def test_auto_commit_creates_checkpoint_commits_in_clean_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=None)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertTrue(auto_commit.enabled)

            (repo_root / ".agent").mkdir()
            (repo_root / ".agent" / "execplan-pending.md").write_text("plan\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(label="find-refactor-candidates", skill_name="find-refactor-candidates"),
                stage_index=1,
                improvement_count=1,
                review_count=1,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "log", "--format=%s", "-1"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "initial",
            )

            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(label="execplan-improve-subagents-1", skill_name="execplan-improve-subagents"),
                stage_index=4,
                improvement_count=1,
                review_count=1,
            )

            (repo_root / "app.py").write_text("print('implemented')\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(label="implement-execplan", skill_name="implement-execplan"),
                stage_index=5,
                improvement_count=1,
                review_count=1,
            )

            (repo_root / "notes.txt").write_text("final review changes\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(
                    label="review-recent-work-subagents-1",
                    skill_name="review-recent-work-subagents",
                ),
                stage_index=6,
                improvement_count=1,
                review_count=1,
            )

            (repo_root / "scratch.txt").write_text("leftover cleanup\n", encoding="utf-8")
            maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: final checkpoint")
        finally:
            run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-5"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        self.assertEqual(
            history[:5],
            [
                "slop-janitor: final checkpoint",
                "slop-janitor: after review-recent-work-subagents-1",
                "slop-janitor: after implement-execplan",
                "slop-janitor: after execplan-improve-subagents-1",
                "initial",
            ],
        )

    def test_non_final_review_stage_does_not_checkpoint(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=None)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertTrue(auto_commit.enabled)

            (repo_root / "review-fix.txt").write_text("review change\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(
                    label="cycle-1-review-recent-work-subagents-1",
                    skill_name="review-recent-work-subagents",
                ),
                stage_index=5,
                improvement_count=0,
                review_count=2,
            )
        finally:
            run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(
            history,
            "initial",
        )

    def test_final_review_stage_creates_checkpoint_commit_in_clean_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=None)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertTrue(auto_commit.enabled)

            (repo_root / "review-fix.txt").write_text("review change\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(
                    label="cycle-1-review-recent-work-subagents-2",
                    skill_name="review-recent-work-subagents",
                ),
                stage_index=6,
                improvement_count=0,
                review_count=2,
            )
        finally:
            run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-2"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        self.assertEqual(
            history[:2],
            [
                "slop-janitor: after cycle-1-review-recent-work-subagents-2",
                "initial",
            ],
        )

    def test_final_non_subagent_review_stage_creates_checkpoint_commit_in_clean_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=None)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertTrue(auto_commit.enabled)

            (repo_root / "review-fix.txt").write_text("review change\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(
                    label="cycle-1-review-recent-work-2",
                    skill_name="review-recent-work",
                ),
                stage_index=6,
                improvement_count=0,
                review_count=2,
            )
        finally:
            run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-2"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        self.assertEqual(
            history[:2],
            [
                "slop-janitor: after cycle-1-review-recent-work-2",
                "initial",
            ],
        )

    def test_dirty_repo_fails_fast_before_auto_commit_setup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        (repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=PROMPT)
        with self.assertRaisesRegex(
            AppServerError,
            "refusing to start: primary repo .* has pre-existing changes",
        ):
            prepare_auto_commit_state(repo_root, run_logger)
        run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(history, "initial")

    def test_dirty_linked_repo_fails_fast_before_run_starts(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        (studio_root / "README.md").write_text("dirty\n", encoding="utf-8")
        prompt = f"Treat {cloud_root} and {studio_root} as one project"
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=prompt)
        try:
            with self.assertRaisesRegex(
                AppServerError,
                "refusing to start: linked repo .*openclaw-studio-private.* has pre-existing changes",
            ):
                prepare_auto_commit_states(cloud_root, prompt, run_logger)
        finally:
            run_logger.close()

    def test_explicit_linked_repo_path_must_exist(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        cloud_root.mkdir()
        self.init_git_repo(cloud_root)
        missing_root = workspace_root / "missing-repo"
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=PROMPT)
        try:
            with self.assertRaisesRegex(AppServerError, "linked repo path does not exist"):
                prepare_auto_commit_states(
                    cloud_root,
                    PROMPT,
                    run_logger,
                    linked_repo_paths=[str(missing_root)],
                )
        finally:
            run_logger.close()

    def test_explicit_linked_repo_path_must_be_git_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        linked_root = workspace_root / "plain-dir"
        cloud_root.mkdir()
        linked_root.mkdir()
        self.init_git_repo(cloud_root)
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=PROMPT)
        try:
            with self.assertRaisesRegex(AppServerError, "linked repo path is not inside a git repository"):
                prepare_auto_commit_states(
                    cloud_root,
                    PROMPT,
                    run_logger,
                    linked_repo_paths=[str(linked_root)],
                )
        finally:
            run_logger.close()

    def test_backticked_prompt_repo_paths_are_discovered(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        prompt = f"Treat `{cloud_root}` and `{studio_root}` as one project"
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=prompt)
        try:
            auto_commits = prepare_auto_commit_states(cloud_root, prompt, run_logger)
        finally:
            run_logger.close()

        self.assertEqual(
            {auto_commit.repo_root.resolve(strict=False) for auto_commit in auto_commits},
            {
                cloud_root.resolve(strict=False),
                studio_root.resolve(strict=False),
            },
        )

    def test_explicit_linked_repo_paths_are_managed_without_prompt_parsing(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=PROMPT)
        try:
            auto_commits = prepare_auto_commit_states(
                cloud_root,
                PROMPT,
                run_logger,
                linked_repo_paths=[str(studio_root)],
            )
        finally:
            run_logger.close()

        self.assertEqual(
            {auto_commit.repo_root.resolve(strict=False) for auto_commit in auto_commits},
            {
                cloud_root.resolve(strict=False),
                studio_root.resolve(strict=False),
            },
        )

    def test_auto_commit_pushes_final_checkpoint_to_tracking_remote(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        remote_root = self.init_git_remote(repo_root)
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="refactor", prompt=PROMPT)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertTrue(auto_commit.enabled)

            (repo_root / ".agent").mkdir()
            (repo_root / ".agent" / "execplan-pending.md").write_text("plan\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(label="execplan-create", skill_name="execplan-create"),
                stage_index=1,
                improvement_count=0,
                review_count=0,
            )

            (repo_root / "notes.txt").write_text("final review changes\n", encoding="utf-8")
            maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: final checkpoint")
            maybe_push_checkpoints([auto_commit], run_logger)
        finally:
            run_logger.close()

        self.assert_remote_head_matches_local(repo_root, remote_root)

    def test_enabled_linked_repo_must_stay_clean_outside_pending_execplan(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        prompt = f"Treat {cloud_root} and {studio_root} as one project"
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=prompt)
        try:
            auto_commits = prepare_auto_commit_states(cloud_root, prompt, run_logger)
            self.assertEqual(len(auto_commits), 2)
            self.assertTrue(all(auto_commit.enabled for auto_commit in auto_commits))

            (cloud_root / ".agent").mkdir()
            (cloud_root / ".agent" / "execplan-pending.md").write_text("plan\n", encoding="utf-8")
            ensure_auto_commit_workspaces_clean(
                auto_commits,
                cloud_root,
                mock.Mock(label="cycle-1-implement-execplan", skill_name="implement-execplan"),
                phase="start",
            )

            (studio_root / "review-fix.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AppServerError,
                "auto-managed repo .*openclaw-studio-private.*has local changes",
            ):
                ensure_auto_commit_workspaces_clean(
                    auto_commits,
                    cloud_root,
                    mock.Mock(label="cycle-1-implement-execplan", skill_name="implement-execplan"),
                    phase="start",
                )
        finally:
            run_logger.close()

    def test_auto_commit_can_checkpoint_prompt_linked_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        cloud_remote = self.init_git_remote(cloud_root)
        studio_remote = self.init_git_remote(studio_root)
        prompt = f"Treat {cloud_root} and {studio_root} as one project"
        run_logger = RunLogger(cloud_root / "run.log", run_cwd=cloud_root, mode="refactor", prompt=prompt)
        try:
            auto_commits = prepare_auto_commit_states(cloud_root, prompt, run_logger)
            self.assertEqual(len(auto_commits), 2)
            self.assertTrue(all(auto_commit.enabled for auto_commit in auto_commits))

            (cloud_root / ".agent").mkdir()
            (cloud_root / ".agent" / "execplan-pending.md").write_text("plan\n", encoding="utf-8")
            (studio_root / "studio-plan.txt").write_text("plan\n", encoding="utf-8")
            maybe_commit_for_stages(
                auto_commits,
                run_logger,
                mock.Mock(label="execplan-create", skill_name="execplan-create"),
                stage_index=3,
                improvement_count=0,
                review_count=0,
            )

            (cloud_root / "app.py").write_text("print('cloud')\n", encoding="utf-8")
            (studio_root / "studio.py").write_text("print('studio')\n", encoding="utf-8")
            maybe_commit_for_stages(
                auto_commits,
                run_logger,
                mock.Mock(label="implement-execplan", skill_name="implement-execplan"),
                stage_index=8,
                improvement_count=4,
                review_count=0,
            )

            (cloud_root / "notes.txt").write_text("final\n", encoding="utf-8")
            (studio_root / "notes.txt").write_text("final\n", encoding="utf-8")
            maybe_commit_checkpoints(auto_commits, run_logger, "slop-janitor: final checkpoint")
            maybe_push_checkpoints(auto_commits, run_logger)
        finally:
            run_logger.close()

        for repo_root in (cloud_root, studio_root):
            history = subprocess.run(
                ["git", "log", "--format=%s", "-4"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().splitlines()
            self.assertEqual(
                history[:4],
                [
                    "slop-janitor: final checkpoint",
                    "slop-janitor: after implement-execplan",
                    "slop-janitor: after execplan-create",
                    "initial",
                ],
            )
        self.assert_remote_head_matches_local(cloud_root, cloud_remote)
        self.assert_remote_head_matches_local(studio_root, studio_remote)

    def test_review_stage_changes_in_linked_repo_are_checkpointed_before_next_cycle(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        prompt = f"Treat {cloud_root} and {studio_root} as one project"

        exit_code, _, stderr, _ = self.run_pipeline(
            "review_mutates_linked_repo",
            argv=[
                "--prompt",
                prompt,
                "--cycles",
                "2",
                "--improvements",
                "0",
                "--review",
                "1",
            ],
            target_cwd=cloud_root,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        history = subprocess.run(
            ["git", "log", "--format=%s", "-6"],
            cwd=studio_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        self.assertIn("slop-janitor: after cycle-1-review-recent-work-1", history)
        self.assertIn("slop-janitor: after cycle-2-review-recent-work-1", history)

    def test_non_subagent_review_stage_changes_in_linked_repo_are_checkpointed_before_next_cycle(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)
        prompt = f"Treat {cloud_root} and {studio_root} as one project"

        exit_code, _, stderr, _ = self.run_pipeline(
            "review_mutates_linked_repo",
            argv=[
                "--prompt",
                prompt,
                "--cycles",
                "2",
                "--improvements",
                "0",
                "--review",
                "1",
                "--review-skill",
                "review-recent-work",
            ],
            target_cwd=cloud_root,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        history = subprocess.run(
            ["git", "log", "--format=%s", "-6"],
            cwd=studio_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        self.assertIn("slop-janitor: after cycle-1-review-recent-work-1", history)
        self.assertIn("slop-janitor: after cycle-2-review-recent-work-1", history)

    def test_default_workflow_runs_candidate_selection_flow_with_prompt(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "refactor_with_prompt",
            argv=["--prompt", PROMPT],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(turn_starts), 6)
        self.assertNotIn("=== Stage 1/6: find-refactor-candidates ===", stdout)
        self.assertIn("========== Workflow Cycle 1/1 ==========", stdout)
        self.assertIn("--- Refactor Discovery ---", stdout)
        self.assertIn("Stage 1/6 · find-refactor-candidates", stdout)
        self.assertIn("=== Stage 1/6: find-refactor-candidates ===", log_text)
        self.assertIn("=== Stage 6/6: review-recent-work-1 ===", log_text)
        text_input = turn_starts[0]["params"]["input"][0]
        skill_input = turn_starts[0]["params"]["input"][1]
        self.assertEqual(text_input["text"], f"$find-refactor-candidates {PROMPT}")
        self.assertEqual(skill_input["name"], "find-refactor-candidates")
        self.assertTrue(skill_input["path"].endswith("/find-refactor-candidates/SKILL.md"))
        self.assertEqual(
            turn_starts[1]["params"]["input"][0]["text"],
            "$select-refactor pressure-test the active shortlist, lock the best refactor decision, and stop before planning.",
        )
        self.assertEqual(
            turn_starts[3]["params"]["input"][0]["text"],
            "$execplan-improve improve the active work-item ExecPlan and rewrite it in place",
        )

    def test_default_workflow_allows_selecting_non_subagent_follow_up_skills(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "refactor_with_prompt",
            argv=[
                "--prompt",
                PROMPT,
                "--improve-skill",
                "execplan-improve",
                "--review-skill",
                "review-recent-work",
                "--improvements",
                "1",
                "--review",
                "1",
            ],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("=== Stage 4/6: execplan-improve-1 ===", log_text)
        self.assertIn("=== Stage 6/6: review-recent-work-1 ===", log_text)
        self.assertEqual(
            turn_starts[3]["params"]["input"][0]["text"],
            "$execplan-improve improve the active work-item ExecPlan and rewrite it in place",
        )
        self.assertEqual(
            turn_starts[5]["params"]["input"][0]["text"],
            "$review-recent-work review the most recently implemented work-item ExecPlan",
        )

    def test_default_workflow_allows_missing_prompt(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "refactor_without_prompt",
            argv=[],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("=== Stage 1/6: find-refactor-candidates ===", stdout)
        self.assertIn("=== Stage 1/6: find-refactor-candidates ===", log_text)
        self.assertEqual(
            turn_start["params"]["input"][0]["text"],
            f"$find-refactor-candidates {DEFAULT_REFACTOR_PROMPT}",
        )

    def test_custom_cycles_improvements_and_review_counts(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "happy_path",
            argv=[
                "--prompt",
                PROMPT,
                "--cycles",
                "2",
                "--improvements",
                "2",
                "--review",
                "1",
            ],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(turn_starts), 14)
        self.assertIn("========== Workflow Cycle 1/2 ==========", stdout)
        self.assertIn("========== Workflow Cycle 2/2 ==========", stdout)
        self.assertIn("--- Improvement Pass 1/2 ---", stdout)
        self.assertIn("--- Review Pass 1/1 ---", stdout)
        self.assertIn("=== Stage 1/14: cycle-1-find-refactor-candidates ===", log_text)
        self.assertIn("=== Stage 14/14: cycle-2-review-recent-work-1 ===", log_text)
        self.assertEqual(turn_starts[0]["params"]["input"][0]["text"], f"$find-refactor-candidates {PROMPT}")
        self.assertEqual(
            turn_starts[1]["params"]["input"][0]["text"],
            "$select-refactor pressure-test the active shortlist, lock the best refactor decision, and stop before planning.",
        )
        self.assertEqual(
            turn_starts[3]["params"]["input"][0]["text"],
            "$execplan-improve improve the active work-item ExecPlan and rewrite it in place",
        )
        self.assertEqual(
            turn_starts[6]["params"]["input"][0]["text"],
            "$review-recent-work review the most recently implemented work-item ExecPlan",
        )
        self.assertEqual(
            turn_starts[7]["params"]["input"][0]["text"],
            f"$find-refactor-candidates {PROMPT}",
        )

    def test_delay_between_cycles_sleeps_once_per_completed_cycle_boundary(self) -> None:
        with mock.patch("slop_janitor.cli.time.sleep") as sleep:
            exit_code, _, stderr, record_path = self.run_pipeline(
                "happy_path",
                argv=[
                    "--prompt",
                    PROMPT,
                    "--cycles",
                    "2",
                    "--improvements",
                    "1",
                    "--review",
                    "1",
                    "--delay-between-cycles-minutes",
                    "0.5",
                ],
            )

        _, log_text = self.read_run_log(record_path)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        sleep.assert_called_once_with(30.0)
        self.assertIn("delayBetweenCyclesMinutes=0.5", log_text)
        self.assertIn("Sleeping 0.5 minute(s) before the next cycle.", log_text)

    def test_happy_path_streams_tokens_and_command_output(self) -> None:
        target_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(target_dir, ignore_errors=True))
        exit_code, stdout, stderr, record_path = self.run_pipeline("happy_path", target_cwd=target_dir)
        record = self.read_json(record_path)
        log_path, log_text = self.read_run_log(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIsNone(record["error"])
        self.assertTrue(log_path.name.startswith(f"{target_dir.name}-"))
        self.assertIn("Starting Codex app-server...", stdout)
        self.assertIn("This can take a bit on the first run while Cargo compiles the Codex workspace.", stdout)
        self.assertIn("Codex app-server ready.", stdout)
        self.assertIn("========== Workflow Cycle 1/1 ==========", stdout)
        self.assertIn("--- Refactor Discovery ---", stdout)
        self.assertIn("[Response]", stdout)
        self.assertIn("Planning stage 1.", stdout)
        self.assertNotIn("running tests", stdout)
        self.assertNotIn("[fileChange] wrote files", stdout)
        self.assertNotIn("[mcp] Tool progress", stdout)
        self.assertNotIn("[started]", stdout)
        self.assertNotIn("=== Stage 1/4", stdout)
        self.assertIn("Tokens this turn: total=100 input=10 cached=1 output=20 reasoning=5", stdout)
        self.assertIn("Tokens cumulative: total=600 input=60 cached=6 output=120 reasoning=30", stdout)
        self.assertIn("=== Stage 1/6: find-refactor-candidates ===", log_text)
        self.assertIn("running tests", log_text)
        self.assertIn("[fileChange] wrote files", log_text)
        self.assertIn("[mcp] Tool progress", log_text)
        self.assertIn("[started] commandExecution pytest -q", log_text)
        self.assertIn("Tokens this turn: total=100 input=10 cached=1 output=20 reasoning=5", log_text)

    def test_initialize_enables_experimental_api_and_starts_thread_once(self) -> None:
        exit_code, _, _, record_path = self.run_pipeline("happy_path")
        record = self.read_json(record_path)
        inbound = self.inbound_messages(record)
        methods = [message.get("method") for message in inbound if "method" in message]

        self.assertEqual(exit_code, 0)
        self.assertEqual(methods[:4], ["initialize", "initialized", "account/read", "thread/start"])
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("turn/start"), 6)
        initialize = next(message for message in inbound if message.get("method") == "initialize")
        self.assertTrue(initialize["params"]["capabilities"]["experimentalApi"])

    def test_thread_start_requests_workspace_write_sandbox(self) -> None:
        exit_code, _, _, record_path = self.run_pipeline("happy_path")
        record = self.read_json(record_path)
        thread_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "thread/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(thread_start["params"]["sandbox"], "workspace-write")
        self.assertEqual(
            thread_start["params"]["config"]["sandbox_workspace_write"]["writable_roots"],
            [thread_start["params"]["cwd"]],
        )
        self.assertFalse(thread_start["params"]["config"]["sandbox_workspace_write"]["network_access"])

    def test_thread_start_requests_workspace_write_for_explicit_linked_repos(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        cloud_root = workspace_root / "openclaw-cloud"
        studio_root = workspace_root / "openclaw-studio-private"
        cloud_root.mkdir()
        studio_root.mkdir()
        self.init_git_repo(cloud_root)
        self.init_git_repo(studio_root)

        exit_code, _, stderr, record_path = self.run_pipeline(
            "happy_path",
            argv=[
                "--prompt",
                PROMPT,
                "--linked-repo",
                str(studio_root),
            ],
            target_cwd=cloud_root,
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        thread_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "thread/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(thread_start["params"]["sandbox"], "workspace-write")
        self.assertEqual(
            thread_start["params"]["config"]["sandbox_workspace_write"]["writable_roots"],
            [str(cloud_root.resolve(strict=False)), str(studio_root.resolve(strict=False))],
        )
        self.assertIn(f"managedRepo1={cloud_root.resolve(strict=False)}", log_text)
        self.assertIn(f"managedRepo2={studio_root.resolve(strict=False)}", log_text)
        self.assertIn(f"sandboxWritableRoot1={cloud_root.resolve(strict=False)}", log_text)
        self.assertIn(f"sandboxWritableRoot2={studio_root.resolve(strict=False)}", log_text)

    def test_thread_start_can_request_danger_full_access(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "happy_path",
            argv=["--prompt", PROMPT, "--sandbox", "danger-full-access"],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        thread_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "thread/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(thread_start["params"]["sandbox"], "danger-full-access")
        self.assertNotIn("config", thread_start["params"])
        self.assertIn("sandboxMode=danger-full-access", log_text)

    def test_multi_cycle_run_starts_a_fresh_thread_each_cycle(self) -> None:
        exit_code, _, _, record_path = self.run_pipeline(
            "happy_path",
            argv=[
                "--prompt",
                PROMPT,
                "--cycles",
                "2",
                "--improvements",
                "1",
                "--review",
                "1",
            ],
        )
        record = self.read_json(record_path)
        inbound = self.inbound_messages(record)
        methods = [message.get("method") for message in inbound if "method" in message]

        self.assertEqual(exit_code, 0)
        self.assertEqual(methods.count("thread/start"), 2)
        self.assertEqual(methods.count("turn/start"), 12)

    def test_workflow_fails_when_cycle_does_not_create_pending_execplan(self) -> None:
        exit_code, _, stderr, _ = self.run_pipeline(
            "refactor_missing_execplan",
            argv=["--prompt", PROMPT],
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("did not produce", stderr)

    def test_thread_started_notification_does_not_break_first_turn(self) -> None:
        exit_code, _, _, record_path = self.run_pipeline("happy_path")
        record = self.read_json(record_path)
        outbound_methods = [
            entry["message"].get("method")
            for entry in record["transcript"]
            if entry["direction"] == "out" and "method" in entry["message"]
        ]

        self.assertEqual(exit_code, 0)
        self.assertIn("thread/started", outbound_methods)

    def test_jsonrpc_error_response_fails_request_cleanly(self) -> None:
        exit_code, _, stderr, _ = self.run_pipeline("jsonrpc_error_turn_start")

        self.assertEqual(exit_code, 1)
        self.assertIn("turn/start` failed with JSON-RPC error 4100", stderr)

    def test_retryable_stage_error_retries_same_thread_and_succeeds(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "retryable_stage_error",
            argv=["--prompt", PROMPT, "--retry-initial-delay-seconds", "0.01", "--retry-max-delay-seconds", "0.01"],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        inbound = self.inbound_messages(record)
        methods = [message.get("method") for message in inbound if "method" in message]

        self.assertEqual(exit_code, 0)
        self.assertIn("retrying stage `find-refactor-candidates`", stderr)
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("turn/start"), 7)
        self.assertIn("[retry] waiting 0.0 second(s) before attempt 2", log_text)

    def test_reader_error_restarts_app_server_and_recovers(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "reader_error_then_recover",
            argv=["--prompt", PROMPT, "--retry-initial-delay-seconds", "0.01", "--retry-max-delay-seconds", "0.01"],
        )
        record = self.read_json(record_path)
        inbound = self.inbound_messages(record)
        methods = [message.get("method") for message in inbound if "method" in message]

        self.assertEqual(exit_code, 0)
        self.assertIn("restarting Codex app-server", stderr)
        self.assertGreaterEqual(methods.count("initialize"), 2)
        self.assertGreaterEqual(methods.count("thread/start"), 2)

    def test_hanging_turn_start_times_out_and_recovers(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "hanging_turn_start_then_recover",
            argv=[
                "--prompt",
                PROMPT,
                "--stage-idle-timeout-seconds",
                "0.05",
                "--retry-initial-delay-seconds",
                "0.01",
                "--retry-max-delay-seconds",
                "0.01",
            ],
        )
        record = self.read_json(record_path)
        inbound = self.inbound_messages(record)
        methods = [message.get("method") for message in inbound if "method" in message]

        self.assertEqual(exit_code, 0)
        self.assertIn("turn/run timed out after 0.1s", stderr)
        self.assertGreaterEqual(methods.count("initialize"), 2)
        self.assertGreaterEqual(methods.count("thread/start"), 2)

    def test_terminal_failure_does_not_use_postcondition_recovery(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
            "approval_request_after_plan_refresh",
            argv=["--prompt", PROMPT],
        )
        record = self.read_json(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(turn_starts), 1)
        self.assertIn("unexpected command approval request", stderr)

    def test_retryable_postcondition_recovery_requires_token_usage(self) -> None:
        exit_code, _, stderr, _ = self.run_pipeline(
            "retryable_impl_postcondition_missing_tokens",
            argv=["--prompt", PROMPT],
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("satisfied recovery postconditions but did not report token usage", stderr)

    def test_retryable_postcondition_recovery_can_continue_with_token_usage(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "retryable_impl_postcondition_success",
            argv=["--prompt", PROMPT],
        )
        record = self.read_json(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(turn_starts), 6)
        self.assertIn("continuing after transient failure in stage `implement-execplan`", stdout)

    def test_fake_server_spawn_override_drives_cli_path(self) -> None:
        server_cwd = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(server_cwd, ignore_errors=True))
        exit_code, _, _, record_path = self.run_pipeline(
            "missing_auth",
            server_cwd=server_cwd,
        )
        record = self.read_json(record_path)

        self.assertEqual(exit_code, 1)
        self.assertEqual(Path(record["serverCwd"]).resolve(), server_cwd.resolve())

    def test_missing_skill_fails_fast(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        record_path = Path(tempdir.name) / "missing-skill.json"
        missing_path = Path(tempdir.name) / "missing-skill.md"
        stderr = io.StringIO()
        with mock.patch.dict(
            "slop_janitor.cli.SKILL_PATHS",
            {"execplan-create": missing_path},
            clear=False,
        ):
            with contextlib.redirect_stderr(stderr):
                exit_code = run(
                    ["--prompt", PROMPT],
                    spawn_spec=self.make_app_server_spawn_spec("missing_auth", record_path),
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("required skill path is missing", stderr.getvalue())
        self.assertFalse(record_path.exists())

    def test_build_refactor_stage_uses_default_prompt_when_missing(self) -> None:
        stages = build_refactor_stages(
            None,
            cycles=1,
            improvement_count=4,
            review_count=5,
            improve_skill_name="execplan-improve-subagents",
            review_skill_name="review-recent-work-subagents",
        )

        self.assertEqual(len(stages), 13)
        self.assertEqual(stages[0].label, "find-refactor-candidates")
        self.assertEqual(
            stages[0].text,
            f"$find-refactor-candidates {DEFAULT_REFACTOR_PROMPT}",
        )
        self.assertEqual(stages[1].label, "select-refactor")
        self.assertEqual(stages[2].label, "execplan-create")
        self.assertEqual(stages[3].label, "execplan-improve-subagents-1")

    def test_build_refactor_stage_forbids_implementation_during_planning_stage(self) -> None:
        stages = build_refactor_stages(
            PROMPT,
            cycles=1,
            improvement_count=4,
            review_count=5,
            improve_skill_name="execplan-improve-subagents",
            review_skill_name="review-recent-work-subagents",
        )

        self.assertEqual(stages[0].text, f"$find-refactor-candidates {PROMPT}")
        self.assertEqual(
            stages[1].text,
            "$select-refactor pressure-test the active shortlist, lock the best refactor decision, and stop before planning.",
        )
        self.assertEqual(
            stages[2].text,
            "$execplan-create create an ExecPlan for the active refactor work item and write it into that work item",
        )

    def test_build_stages_respects_custom_counts(self) -> None:
        stages = build_stages(
            PROMPT,
            cycles=2,
            improvement_count=1,
            review_count=2,
            improve_skill_name="execplan-improve-subagents",
            review_skill_name="review-recent-work-subagents",
        )

        self.assertEqual(len(stages), 14)
        self.assertEqual(stages[0].label, "cycle-1-find-refactor-candidates")
        self.assertEqual(stages[4].label, "cycle-1-implement-execplan")
        self.assertEqual(stages[5].label, "cycle-1-review-recent-work-subagents-1")
        self.assertEqual(stages[-1].label, "cycle-2-review-recent-work-subagents-2")

    def test_build_stages_respects_selected_follow_up_skills(self) -> None:
        stages = build_stages(
            PROMPT,
            cycles=1,
            improvement_count=1,
            review_count=1,
            improve_skill_name="execplan-improve",
            review_skill_name="review-recent-work",
        )

        self.assertEqual([stage.label for stage in stages], [
            "find-refactor-candidates",
            "select-refactor",
            "execplan-create",
            "execplan-improve-1",
            "implement-execplan",
            "review-recent-work-1",
        ])

    def test_unexpected_approval_request_declines_and_fails(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline("approval_request")
        record = self.read_json(record_path)
        client_response = next(
            message
            for message in self.inbound_messages(record)
            if message.get("id") == 900 and "result" in message
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(client_response["result"], {"decision": "decline"})
        self.assertIn("unexpected command approval request", stderr)

    def test_failed_turn_stops_pipeline(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline("failed_turn")
        record = self.read_json(record_path)
        turn_starts = [
            message
            for message in self.inbound_messages(record)
            if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(turn_starts), 1)
        self.assertIn("stage failed", stderr)

    def test_retryable_failure_with_workspace_changes_stops_as_ambiguous(self) -> None:
        target_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(target_dir, ignore_errors=True))
        self.init_git_repo(target_dir)

        exit_code, _, stderr, record_path = self.run_pipeline(
            "retryable_impl_ambiguity",
            argv=["--prompt", PROMPT, "--retry-initial-delay-seconds", "0.01", "--retry-max-delay-seconds", "0.01"],
            target_cwd=target_dir,
        )
        record = self.read_json(record_path)
        turn_starts = [
            message
            for message in self.inbound_messages(record)
            if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(turn_starts), 5)
        self.assertIn("left workspace changes that make replay unsafe", stderr)


if __name__ == "__main__":
    unittest.main()

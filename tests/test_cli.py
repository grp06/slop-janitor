from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from slop_janitor.app_server import AppServerSpawnSpec
from slop_janitor.cli import build_refactor_stages
from slop_janitor.cli import build_stages
from slop_janitor.cli import main
from slop_janitor.cli import maybe_commit_checkpoint
from slop_janitor.cli import maybe_commit_for_stage
from slop_janitor.cli import prepare_auto_commit_state
from slop_janitor.cli import resolve_codex_workspace
from slop_janitor.cli import run
from slop_janitor.cli import run_auth
from slop_janitor.run_log import RunLogger


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_app_server.py"
FAKE_CODEX_CLI = REPO_ROOT / "tests" / "fixtures" / "fake_codex_cli.py"
PROMPT = "help me build a CRM"


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

    def run_pipeline(
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
        stdout = io.StringIO()
        stderr = io.StringIO()
        cli_argv = argv or ["--prompt", PROMPT]
        mode = "pipeline"
        prompt: str | None = PROMPT
        cycles = 1
        improvements = 4
        review = 5
        index = 0
        while index < len(cli_argv):
            token = cli_argv[index]
            if token == "--mode":
                mode = cli_argv[index + 1]
                index += 2
                continue
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
            if token == "--review":
                review = int(cli_argv[index + 1])
                index += 2
                continue
            index += 1
        if mode == "refactor" and "--prompt" not in cli_argv:
            prompt = None
        config_path.write_text(
            json.dumps(
                {
                    "mode": mode,
                    "prompt": prompt,
                    "cycles": cycles,
                    "improvements": improvements,
                    "review": review,
                }
            ),
            encoding="utf-8",
        )
        with chdir(target_cwd or REPO_ROOT):
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
        self.addCleanup(target_dir.rmdir)
        exit_code, _, stderr, record_path = self.run_pipeline("missing_auth", target_cwd=target_dir)
        record = self.read_json(record_path)
        methods = [message["method"] for message in self.inbound_messages(record) if "method" in message]

        self.assertEqual(exit_code, 1)
        self.assertIn("slop-janitor auth login", stderr)
        self.assertNotIn("thread/start", methods)

    def test_pipeline_mode_requires_prompt(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run([])

        self.assertEqual(exit_code, 1)
        self.assertIn("`--prompt` is required when `--mode pipeline` is selected", stderr.getvalue())

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
                mock.Mock(label="find-best-refactor", skill_name="find-best-refactor"),
                stage_index=1,
            )

            (repo_root / "app.py").write_text("print('implemented')\n", encoding="utf-8")
            maybe_commit_for_stage(
                auto_commit,
                run_logger,
                mock.Mock(label="implement-execplan", skill_name="implement-execplan"),
                stage_index=6,
            )

            (repo_root / "notes.txt").write_text("final review changes\n", encoding="utf-8")
            maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: final checkpoint")
        finally:
            run_logger.close()

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
                "slop-janitor: initial plan created",
                "initial",
            ],
        )

    def test_auto_commit_is_disabled_for_dirty_repo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        self.init_git_repo(repo_root)
        (repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
        run_logger = RunLogger(repo_root / "run.log", run_cwd=repo_root, mode="pipeline", prompt=PROMPT)
        try:
            auto_commit = prepare_auto_commit_state(repo_root, run_logger)
            self.assertFalse(auto_commit.enabled)
            maybe_commit_checkpoint(auto_commit, run_logger, "slop-janitor: final checkpoint")
        finally:
            run_logger.close()

        history = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(history, "initial")

    def test_refactor_mode_runs_find_best_refactor_with_prompt(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "refactor_with_prompt",
            argv=["--mode", "refactor", "--prompt", PROMPT],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_starts = [
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(turn_starts), 11)
        self.assertNotIn("=== Stage 1/11: find-best-refactor ===", stdout)
        self.assertIn("=== Stage 1/11: find-best-refactor ===", log_text)
        self.assertIn("=== Stage 11/11: review-recent-work-5 ===", log_text)
        text_input = turn_starts[0]["params"]["input"][0]
        skill_input = turn_starts[0]["params"]["input"][1]
        self.assertEqual(text_input["text"], f"$find-best-refactor {PROMPT}")
        self.assertEqual(skill_input["name"], "find-best-refactor")
        self.assertTrue(skill_input["path"].endswith("/find-best-refactor/SKILL.md"))
        self.assertEqual(
            turn_starts[1]["params"]["input"][0]["text"],
            "$execplan-improve improve the pending execution plan at .agent/execplan-pending.md",
        )

    def test_refactor_mode_allows_missing_prompt(self) -> None:
        exit_code, stdout, stderr, record_path = self.run_pipeline(
            "refactor_without_prompt",
            argv=["--mode", "refactor"],
        )
        record = self.read_json(record_path)
        _, log_text = self.read_run_log(record_path)
        turn_start = next(
            message for message in self.inbound_messages(record) if message.get("method") == "turn/start"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("=== Stage 1/11: find-best-refactor ===", stdout)
        self.assertIn("=== Stage 1/11: find-best-refactor ===", log_text)
        self.assertEqual(
            turn_start["params"]["input"][0]["text"],
            "$find-best-refactor find the single highest-leverage refactor in this repository",
        )

    def test_custom_cycles_improvements_and_review_counts(self) -> None:
        exit_code, _, stderr, record_path = self.run_pipeline(
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
        self.assertEqual(len(turn_starts), 10)
        self.assertIn("=== Stage 1/10: cycle-1-execplan-create ===", log_text)
        self.assertIn("=== Stage 10/10: cycle-2-review-recent-work-1 ===", log_text)
        self.assertEqual(turn_starts[0]["params"]["input"][0]["text"], f"$execplan-create {PROMPT}")
        self.assertEqual(
            turn_starts[1]["params"]["input"][0]["text"],
            "$execplan-improve improve the pending execution plan at .agent/execplan-pending.md",
        )
        self.assertEqual(
            turn_starts[4]["params"]["input"][0]["text"],
            "$review-recent-work review the most recently implemented ExecPlan work",
        )
        self.assertEqual(
            turn_starts[5]["params"]["input"][0]["text"],
            f"$execplan-create {PROMPT}",
        )

    def test_happy_path_streams_tokens_and_command_output(self) -> None:
        target_dir = Path(tempfile.mkdtemp())
        self.addCleanup(target_dir.rmdir)
        exit_code, stdout, stderr, record_path = self.run_pipeline("happy_path", target_cwd=target_dir)
        record = self.read_json(record_path)
        log_path, log_text = self.read_run_log(record_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIsNone(record["error"])
        self.assertTrue(log_path.name.startswith(f"{target_dir.name}-"))
        self.assertIn("Planning stage 1.", stdout)
        self.assertNotIn("running tests", stdout)
        self.assertNotIn("[fileChange] wrote files", stdout)
        self.assertNotIn("[mcp] Tool progress", stdout)
        self.assertNotIn("[started]", stdout)
        self.assertNotIn("=== Stage 1/11", stdout)
        self.assertIn("Tokens this turn: total=100 input=10 cached=1 output=20 reasoning=5", stdout)
        self.assertIn("Tokens cumulative: total=1100 input=110 cached=11 output=220 reasoning=55", stdout)
        self.assertIn("=== Stage 1/11: execplan-create ===", log_text)
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
        self.assertEqual(methods.count("turn/start"), 11)
        initialize = next(message for message in inbound if message.get("method") == "initialize")
        self.assertTrue(initialize["params"]["capabilities"]["experimentalApi"])

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

    def test_fake_server_spawn_override_drives_cli_path(self) -> None:
        server_cwd = Path(tempfile.mkdtemp())
        self.addCleanup(server_cwd.rmdir)
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
        stages = build_refactor_stages(None, cycles=1, improvement_count=4, review_count=5)

        self.assertEqual(len(stages), 11)
        self.assertEqual(stages[0].label, "find-best-refactor")
        self.assertEqual(
            stages[0].text,
            "$find-best-refactor find the single highest-leverage refactor in this repository",
        )
        self.assertEqual(stages[1].label, "execplan-improve-1")

    def test_build_stages_respects_custom_counts(self) -> None:
        stages = build_stages(
            "pipeline",
            PROMPT,
            cycles=2,
            improvement_count=1,
            review_count=2,
        )

        self.assertEqual(len(stages), 10)
        self.assertEqual(stages[0].label, "cycle-1-execplan-create")
        self.assertEqual(stages[4].label, "cycle-1-review-recent-work-2")
        self.assertEqual(stages[5].label, "cycle-2-execplan-create")
        self.assertEqual(stages[-1].label, "cycle-2-review-recent-work-2")

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


if __name__ == "__main__":
    unittest.main()

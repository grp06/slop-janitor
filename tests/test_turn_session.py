from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from slop_janitor.run_log import RunLogger
from slop_janitor.turn_session import TurnSession


PROMPT = "help me build a CRM"


class TurnSessionTests(unittest.TestCase):
    def make_session(self) -> TurnSession:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_root = Path(tempdir.name)
        run_logger = RunLogger(
            repo_root / "runs" / "turn-session.log",
            run_cwd=repo_root,
            mode="pipeline",
            prompt=PROMPT,
        )
        self.addCleanup(run_logger.close)
        return TurnSession(thread_id="thread-1", turn_id="turn-1", run_logger=run_logger)

    def token_usage_notification(self) -> dict:
        return {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "totalTokens": 100,
                        "inputTokens": 10,
                        "cachedInputTokens": 1,
                        "outputTokens": 20,
                        "reasoningOutputTokens": 5,
                    },
                    "total": {
                        "totalTokens": 1100,
                        "inputTokens": 110,
                        "cachedInputTokens": 11,
                        "outputTokens": 220,
                        "reasoningOutputTokens": 55,
                    },
                },
            },
        }

    def delta_notification(self, *, item_id: str, delta: str) -> dict:
        return {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": item_id,
                "delta": delta,
            },
        }

    def completed_notification(self, *, status: str = "completed", error: dict | None = None) -> dict:
        return {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": status,
                    "error": error,
                },
            },
        }

    def test_assistant_message_item_ids_do_not_interleave(self) -> None:
        session = self.make_session()
        with contextlib.redirect_stdout(io.StringIO()):
            session.handle_notification(self.token_usage_notification())
            session.handle_notification(self.delta_notification(item_id="alpha", delta="alpha one"))
            session.handle_notification(self.delta_notification(item_id="alpha", delta="alpha two"))
            session.handle_notification(self.delta_notification(item_id="beta", delta="beta only"))
            result = session.handle_notification(self.completed_notification())

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.assistant_text, "alpha onealpha twobeta only")
        self.assertIsNotNone(result.token_usage)

    def test_completed_turn_without_token_usage_fails(self) -> None:
        session = self.make_session()
        result = session.handle_notification(self.completed_notification())

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_message, "successful turn completed without token usage data")

    def test_server_request_matrix_matches_noninteractive_policy(self) -> None:
        cases = [
            (
                "item/commandExecution/requestApproval",
                {"decision": "decline"},
                None,
                "received unexpected command approval request while approvalPolicy=never",
            ),
            (
                "item/fileChange/requestApproval",
                {"decision": "decline"},
                None,
                "received unexpected file change approval request while approvalPolicy=never",
            ),
            (
                "item/tool/requestUserInput",
                {"answers": {}},
                None,
                "interactive tool input is unsupported in this pipeline",
            ),
            (
                "mcpServer/elicitation/request",
                {"action": "decline", "content": None, "_meta": None},
                None,
                "MCP elicitation is unsupported in this pipeline",
            ),
            (
                "item/permissions/requestApproval",
                {"permissions": {}, "scope": "turn"},
                None,
                "permission approval is unsupported in this pipeline",
            ),
            (
                "account/chatgptAuthTokens/refresh",
                None,
                "external auth refresh is unsupported in slop-janitor",
                "external auth refresh is unsupported in this pipeline",
            ),
            (
                "unknown/method",
                None,
                "unsupported server request `unknown/method`",
                "received unsupported server request `unknown/method`",
            ),
        ]

        for method, expected_result, expected_error, expected_failure in cases:
            session = self.make_session()
            reply = session.handle_server_request({"id": 900, "method": method})

            self.assertEqual(reply.request_id, 900)
            self.assertEqual(reply.result, expected_result)
            self.assertEqual(reply.error_message, expected_error)
            self.assertEqual(reply.failure_message, expected_failure)
            self.assertEqual(session.failure_message, expected_failure)

    def test_non_retrying_error_notification_poisons_completed_turn(self) -> None:
        session = self.make_session()
        session.handle_notification(self.token_usage_notification())
        session.handle_notification(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {
                        "message": "stage exploded",
                        "codexErrorInfo": "fatal",
                    },
                },
            }
        )
        result = session.handle_notification(self.completed_notification())

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_message, "stage exploded | fatal")


if __name__ == "__main__":
    unittest.main()

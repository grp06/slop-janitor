#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


SKILLS_ROOT = str(Path(__file__).resolve().parents[2] / ".agents" / "skills")
PROMPT = "help me build a CRM"


def build_follow_up_stages(*, improvement_count: int, review_count: int) -> list[dict[str, str]]:
    return [
        *[
            {
                "skill_name": "execplan-improve",
                "skill_path": f"{SKILLS_ROOT}/execplan-improve/SKILL.md",
                "text": "$execplan-improve improve the pending execution plan at .agent/execplan-pending.md",
            }
            for _ in range(improvement_count)
        ],
        {
            "skill_name": "implement-execplan",
            "skill_path": f"{SKILLS_ROOT}/implement-execplan/SKILL.md",
            "text": "$implement-execplan implement the pending execution plan at .agent/execplan-pending.md",
        },
        *[
            {
                "skill_name": "review-recent-work",
                "skill_path": f"{SKILLS_ROOT}/review-recent-work/SKILL.md",
                "text": "$review-recent-work review the most recently implemented ExecPlan work",
            }
            for _ in range(review_count)
        ],
    ]


def build_expected_stages(prompt: str, *, cycles: int, improvement_count: int, review_count: int) -> list[dict[str, str]]:
    stages: list[dict[str, str]] = []
    for _ in range(cycles):
        stages.append(
            {
                "skill_name": "execplan-create",
                "skill_path": f"{SKILLS_ROOT}/execplan-create/SKILL.md",
                "text": f"$execplan-create {prompt}",
            }
        )
        stages.extend(build_follow_up_stages(improvement_count=improvement_count, review_count=review_count))
    return stages


def build_expected_refactor_stages(
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
) -> list[dict[str, str]]:
    text = "$find-best-refactor"
    if prompt:
        text = f"{text} {prompt}"
    else:
        text = f"{text} find the single highest-leverage refactor in this repository"
    stages: list[dict[str, str]] = []
    for _ in range(cycles):
        stages.append(
            {
                "skill_name": "find-best-refactor",
                "skill_path": f"{SKILLS_ROOT}/find-best-refactor/SKILL.md",
                "text": text,
            }
        )
        stages.extend(build_follow_up_stages(improvement_count=improvement_count, review_count=review_count))
    return stages


class ProtocolError(RuntimeError):
    pass


class FakeServer:
    def __init__(self, scenario: str, record_path: Path) -> None:
        self.scenario = scenario
        self.record_path = record_path
        self.transcript: list[dict[str, Any]] = []
        self.thread_id = "thread-1"
        config_path = Path(sys.argv[3]) if len(sys.argv) == 4 else None
        self.config = {
            "mode": "pipeline",
            "prompt": PROMPT,
            "cycles": 1,
            "improvements": 4,
            "review": 5,
        }
        if config_path is not None:
            self.config.update(json.loads(config_path.read_text(encoding="utf-8")))
        if self.config["mode"] == "refactor":
            self.expected_stages = build_expected_refactor_stages(
                self.config.get("prompt"),
                cycles=int(self.config["cycles"]),
                improvement_count=int(self.config["improvements"]),
                review_count=int(self.config["review"]),
            )
        else:
            self.expected_stages = build_expected_stages(
                str(self.config["prompt"]),
                cycles=int(self.config["cycles"]),
                improvement_count=int(self.config["improvements"]),
                review_count=int(self.config["review"]),
            )
        self.error: str | None = None

    def send(self, message: dict[str, Any]) -> None:
        self.transcript.append({"direction": "out", "message": message})
        print(json.dumps(message), flush=True)

    def read(self) -> dict[str, Any]:
        line = sys.stdin.readline()
        if line == "":
            raise EOFError("stdin closed")
        message = json.loads(line)
        self.transcript.append({"direction": "in", "message": message})
        return message

    def expect_request(self, method: str) -> dict[str, Any]:
        message = self.read()
        if message.get("method") != method or "id" not in message:
            raise ProtocolError(f"expected request `{method}`, got {message}")
        return message

    def expect_notification(self, method: str) -> dict[str, Any]:
        message = self.read()
        if message.get("method") != method or "id" in message:
            raise ProtocolError(f"expected notification `{method}`, got {message}")
        return message

    def expect_client_result(self, request_id: int) -> dict[str, Any]:
        message = self.read()
        if message.get("id") != request_id or "result" not in message:
            raise ProtocolError(f"expected JSON-RPC result for {request_id}, got {message}")
        return message

    def expect_client_error(self, request_id: int) -> dict[str, Any]:
        message = self.read()
        if message.get("id") != request_id or "error" not in message:
            raise ProtocolError(f"expected JSON-RPC error for {request_id}, got {message}")
        return message

    def write_record(self) -> None:
        self.record_path.write_text(
            json.dumps(
                {
                    "scenario": self.scenario,
                    "serverCwd": os.getcwd(),
                    "transcript": self.transcript,
                    "error": self.error,
                }
            ),
            encoding="utf-8",
        )

    def run(self) -> int:
        try:
            self._run()
            return 0
        except Exception as exc:
            self.error = str(exc)
            return 2
        finally:
            self.write_record()

    def _run(self) -> None:
        initialize = self.expect_request("initialize")
        params = initialize.get("params", {})
        capabilities = params.get("capabilities", {})
        client_info = params.get("clientInfo", {})
        if capabilities.get("experimentalApi") is not True:
            raise ProtocolError(f"initialize missing experimentalApi opt-in: {params}")
        if client_info.get("name") != "slop-janitor" or client_info.get("title") != "slop-janitor":
            raise ProtocolError(f"unexpected clientInfo: {client_info}")
        self.send(
            {
                "id": initialize["id"],
                "result": {
                    "protocolVersion": "2026-03-17",
                    "serverInfo": {"name": "fake-app-server", "version": "0.1.0"},
                    "capabilities": {},
                },
            }
        )

        self.expect_notification("initialized")

        account_read = self.expect_request("account/read")
        if account_read.get("params") != {"refreshToken": False}:
            raise ProtocolError(f"unexpected account/read params: {account_read}")
        if self.scenario == "missing_auth":
            self.send(
                {
                    "id": account_read["id"],
                    "result": {"account": None, "requiresOpenaiAuth": True},
                }
            )
            return

        self.send(
            {
                "id": account_read["id"],
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": "person@example.com",
                        "planType": "plus",
                    },
                    "requiresOpenaiAuth": True,
                },
            }
        )

        thread_start = self.expect_request("thread/start")
        thread_params = thread_start.get("params", {})
        if thread_params.get("approvalPolicy") != "never":
            raise ProtocolError(f"unexpected approvalPolicy: {thread_params}")
        if not isinstance(thread_params.get("cwd"), str) or not thread_params["cwd"]:
            raise ProtocolError(f"thread/start missing cwd: {thread_params}")
        self.send(
            {
                "id": thread_start["id"],
                "result": {
                    "thread": {"id": self.thread_id},
                    "model": "gpt-5.4",
                    "modelProvider": "openai",
                    "cwd": thread_params["cwd"],
                    "approvalPolicy": "never",
                    "approvalsReviewer": {"type": "cli"},
                    "sandbox": {"mode": "danger-full-access"},
                    "reasoningEffort": "medium",
                },
            }
        )
        self.send({"method": "thread/started", "params": {"thread": {"id": self.thread_id}}})

        if self.scenario == "multi_agent_items":
            self.run_multi_agent_stage()
            return
        if self.scenario == "jsonrpc_error_turn_start":
            self.run_turn_start_error()
            return
        if self.scenario == "non_retrying_error":
            self.run_non_retrying_error_stage()
            return
        if self.scenario == "tool_request_user_input":
            self.run_server_request_stage(
                "item/tool/requestUserInput",
                {
                    "threadId": self.thread_id,
                    "turnId": "turn-1",
                    "itemId": "tool-1",
                    "questions": [],
                },
                expect_result={"answers": {}},
                failure_message="interactive tool input is unsupported in this pipeline",
            )
            return
        if self.scenario == "mcp_elicitation":
            self.run_server_request_stage(
                "mcpServer/elicitation/request",
                {
                    "threadId": self.thread_id,
                    "turnId": "turn-1",
                    "elicitationId": "elic-1",
                    "request": {
                        "type": "form",
                        "message": "Need input",
                        "requestedSchema": {"type": "object"},
                        "_meta": None,
                    },
                },
                expect_result={"action": "decline", "content": None, "_meta": None},
                failure_message="MCP elicitation is unsupported in this pipeline",
            )
            return
        if self.scenario == "permissions_request":
            self.run_server_request_stage(
                "item/permissions/requestApproval",
                {
                    "threadId": self.thread_id,
                    "turnId": "turn-1",
                    "itemId": "perm-1",
                    "permissions": {},
                    "reason": "Need permissions",
                },
                expect_result={"permissions": {}, "scope": "turn"},
                failure_message="permission approval is unsupported in this pipeline",
            )
            return
        if self.scenario == "chatgpt_auth_refresh":
            self.run_server_request_stage(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized", "previousAccountId": None},
                expect_error="external auth refresh is unsupported in slop-janitor",
                failure_message="external auth refresh is unsupported in this pipeline",
            )
            return
        if self.scenario == "approval_request":
            self.run_server_request_stage(
                "item/commandExecution/requestApproval",
                {
                    "threadId": self.thread_id,
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "command": ["git", "status"],
                },
                expect_result={"decision": "decline"},
                failure_message="received unexpected command approval request while approvalPolicy=never",
            )
            return
        if self.scenario == "approval_request_completed_status":
            self.run_server_request_stage(
                "item/commandExecution/requestApproval",
                {
                    "threadId": self.thread_id,
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "command": ["git", "status"],
                },
                expect_result={"decision": "decline"},
                failure_message="received unexpected command approval request while approvalPolicy=never",
                turn_status="completed",
                include_token_usage=True,
            )
            return
        if self.scenario == "failed_turn":
            self.run_failed_turn_stage()
            return
        if self.scenario == "happy_path":
            self.run_happy_path()
            return
        if self.scenario in {"refactor_with_prompt", "refactor_without_prompt"}:
            self.run_happy_path()
            return
        raise ProtocolError(f"unsupported scenario: {self.scenario}")

    def validate_turn_start(self, message: dict[str, Any], stage_index: int) -> tuple[int, str]:
        self.check_turn_start_inputs(message, stage_index)
        turn_id = f"turn-{stage_index + 1}"
        self.send({"id": message["id"], "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        self.send(
            {
                "method": "turn/started",
                "params": {"threadId": self.thread_id, "turn": {"id": turn_id, "status": "inProgress"}},
            }
        )
        return message["id"], turn_id

    def check_turn_start_inputs(self, message: dict[str, Any], stage_index: int) -> None:
        params = message.get("params", {})
        if params.get("threadId") != self.thread_id:
            raise ProtocolError(f"turn/start used wrong thread id: {params}")
        inputs = params.get("input")
        if not isinstance(inputs, list) or len(inputs) != 2:
            raise ProtocolError(f"turn/start expected two inputs: {params}")
        text_item, skill_item = inputs
        expected = self.expected_stages[stage_index]
        if text_item != {"type": "text", "text": expected["text"], "textElements": []}:
            raise ProtocolError(f"unexpected text input for stage {stage_index + 1}: {text_item}")
        if skill_item != {
            "type": "skill",
            "name": expected["skill_name"],
            "path": expected["skill_path"],
        }:
            raise ProtocolError(f"unexpected skill input for stage {stage_index + 1}: {skill_item}")

    def send_token_usage(self, turn_id: str, stage_index: int) -> None:
        base = stage_index + 1
        self.send(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "last": {
                            "totalTokens": 100 * base,
                            "inputTokens": 10 * base,
                            "cachedInputTokens": base,
                            "outputTokens": 20 * base,
                            "reasoningOutputTokens": 5 * base,
                        },
                        "total": {
                            "totalTokens": 100 * base,
                            "inputTokens": 10 * base,
                            "cachedInputTokens": base,
                            "outputTokens": 20 * base,
                            "reasoningOutputTokens": 5 * base,
                        },
                    },
                },
            }
        )

    def complete_turn(self, turn_id: str, *, status: str = "completed", error: dict[str, Any] | None = None) -> None:
        turn: dict[str, Any] = {"id": turn_id, "status": status}
        if error is not None:
            turn["error"] = error
        self.send({"method": "turn/completed", "params": {"threadId": self.thread_id, "turn": turn}})

    def run_happy_path(self) -> None:
        for stage_index in range(len(self.expected_stages)):
            turn_start = self.expect_request("turn/start")
            _, turn_id = self.validate_turn_start(turn_start, stage_index)
            if stage_index == 0:
                self.send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {"type": "agentMessage", "id": "agent-1", "text": "", "phase": "response"},
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "itemId": "agent-1",
                            "delta": "Planning stage 1.\n",
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "commandExecution",
                                "id": "cmd-1",
                                "command": "pytest -q",
                                "cwd": "/tmp/crm-scratch",
                                "status": "inProgress",
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/commandExecution/outputDelta",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "itemId": "cmd-1",
                            "delta": "running tests\n",
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "commandExecution",
                                "id": "cmd-1",
                                "command": "pytest -q",
                                "cwd": "/tmp/crm-scratch",
                                "status": "completed",
                                "exitCode": 0,
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "fileChange",
                                "id": "file-1",
                                "changes": [{"path": "a.py"}, {"path": "b.py"}],
                                "status": "inProgress",
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/fileChange/outputDelta",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "itemId": "file-1",
                            "delta": "wrote files",
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "fileChange",
                                "id": "file-1",
                                "changes": [{"path": "a.py"}, {"path": "b.py"}],
                                "status": "completed",
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "mcpToolCall",
                                "id": "mcp-1",
                                "server": "docs",
                                "tool": "search",
                                "status": "inProgress",
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/mcpToolCall/progress",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "itemId": "mcp-1",
                            "message": "Tool progress",
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "mcpToolCall",
                                "id": "mcp-1",
                                "server": "docs",
                                "tool": "search",
                                "status": "completed",
                            },
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "agent-1",
                                "text": "Planning stage 1.\n",
                                "phase": "response",
                            },
                        },
                    }
                )
            else:
                item_id = f"agent-{stage_index + 1}"
                self.send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {"type": "agentMessage", "id": item_id, "text": "", "phase": "response"},
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "itemId": item_id,
                            "delta": f"Stage {stage_index + 1} output.\n",
                        },
                    }
                )
                self.send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": item_id,
                                "text": f"Stage {stage_index + 1} output.\n",
                                "phase": "response",
                            },
                        },
                    }
                )
            self.send_token_usage(turn_id, stage_index)
            self.complete_turn(turn_id)

    def run_turn_start_error(self) -> None:
        turn_start = self.expect_request("turn/start")
        self.check_turn_start_inputs(turn_start, 0)
        self.send(
            {
                "id": turn_start["id"],
                "error": {"code": 4100, "message": "turn/start rejected by fake server"},
            }
        )

    def run_multi_agent_stage(self) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0)
        self.send(
            {
                "method": "item/started",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "item": {"type": "agentMessage", "id": "agent-a", "text": "", "phase": "response"},
                },
            }
        )
        self.send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "itemId": "agent-a",
                    "delta": "alpha one",
                },
            }
        )
        self.send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "itemId": "agent-a",
                    "delta": "alpha two",
                },
            }
        )
        self.send(
            {
                "method": "item/started",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "item": {"type": "agentMessage", "id": "agent-b", "text": "", "phase": "response"},
                },
            }
        )
        self.send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "itemId": "agent-b",
                    "delta": "beta only",
                },
            }
        )
        self.send_token_usage(turn_id, 0)
        self.complete_turn(turn_id)

    def run_non_retrying_error_stage(self) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0)
        self.send(
            {
                "method": "error",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {
                        "message": "stage exploded",
                        "codexErrorInfo": {"kind": "fatal"},
                        "additionalDetails": {"source": "fake"},
                    },
                },
            }
        )
        self.complete_turn(
            turn_id,
            status="failed",
            error={
                "message": "stage exploded",
                "codexErrorInfo": {"kind": "fatal"},
                "additionalDetails": {"source": "fake"},
            },
        )

    def run_failed_turn_stage(self) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0)
        self.complete_turn(turn_id, status="failed", error={"message": "stage failed"})

    def run_server_request_stage(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_result: dict[str, Any] | None = None,
        expect_error: str | None = None,
        failure_message: str,
        turn_status: str = "failed",
        include_token_usage: bool = False,
    ) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0)
        request_id = 900
        self.send({"id": request_id, "method": method, "params": params})
        if expect_result is not None:
            response = self.expect_client_result(request_id)
            if response.get("result") != expect_result:
                raise ProtocolError(f"unexpected result for {method}: {response}")
        elif expect_error is not None:
            response = self.expect_client_error(request_id)
            error = response.get("error", {})
            if expect_error not in str(error.get("message")):
                raise ProtocolError(f"unexpected error for {method}: {response}")
        else:
            raise ProtocolError("one of expect_result or expect_error is required")
        if include_token_usage:
            self.send_token_usage(turn_id, 0)
        self.complete_turn(turn_id, status=turn_status, error={"message": failure_message})


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit("usage: fake_app_server.py <scenario> <record-path> [config-path]")
    return FakeServer(sys.argv[1], Path(sys.argv[2])).run()


if __name__ == "__main__":
    raise SystemExit(main())

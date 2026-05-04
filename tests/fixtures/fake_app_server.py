#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from slop_janitor import workflow_topology


SKILLS_ROOT = str(Path(__file__).resolve().parents[2] / ".agents" / "skills")
PROMPT = "help me build a CRM"
DEFAULT_REFACTOR_PROMPT = "identify the top materially different refactor candidates in this repository"
SKILL_PATHS = {
    "create-meta-plan": f"{SKILLS_ROOT}/create-meta-plan/SKILL.md",
    "find-refactor-candidates": f"{SKILLS_ROOT}/find-refactor-candidates/SKILL.md",
    "select-refactor": f"{SKILLS_ROOT}/select-refactor/SKILL.md",
    "execplan-create": f"{SKILLS_ROOT}/execplan-create/SKILL.md",
    "execplan-improve": f"{SKILLS_ROOT}/execplan-improve/SKILL.md",
    "implement-execplan": f"{SKILLS_ROOT}/implement-execplan/SKILL.md",
    "review-recent-work": f"{SKILLS_ROOT}/review-recent-work/SKILL.md",
}


def build_expected_refactor_stages(
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
) -> list[dict[str, str]]:
    return [
        {
            "label": stage.label,
            "skill_name": stage.skill_name,
            "skill_path": stage.skill_path,
            "text": stage.text,
        }
        for stage in workflow_topology.build_refactor_stages(
            prompt,
            cycles=cycles,
            improvement_count=improvement_count,
            review_count=review_count,
            skill_paths=SKILL_PATHS,
            default_refactor_prompt=DEFAULT_REFACTOR_PROMPT,
        )
    ]


def build_expected_builder_stages(
    *,
    prompt: str | None,
    slices: int,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool,
) -> list[dict[str, str]]:
    if includes_meta_plan_creation:
        stages = workflow_topology.build_builder_stages(
            str(prompt),
            slices=slices,
            improvement_count=improvement_count,
            review_count=review_count,
            skill_paths=SKILL_PATHS,
        )
    else:
        stages = workflow_topology.build_existing_meta_plan_builder_stages(
            slices=slices,
            improvement_count=improvement_count,
            review_count=review_count,
            skill_paths=SKILL_PATHS,
        )
    return [
        {
            "label": stage.label,
            "skill_name": stage.skill_name,
            "skill_path": stage.skill_path,
            "text": stage.text,
        }
        for stage in stages
    ]


class ProtocolError(RuntimeError):
    pass


class FakeServer:
    def __init__(self, scenario: str, record_path: Path) -> None:
        self.scenario = scenario
        self.record_path = record_path
        self.transcript: list[dict[str, Any]] = []
        previous_record: dict[str, Any] | None = None
        if self.record_path.exists():
            previous_record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.session_index = len(previous_record.get("sessions", [])) if isinstance(previous_record, dict) else 0
        self.thread_id = "thread-0"
        self.thread_count = 0
        self.current_goal: dict[str, Any] | None = None
        self.run_cwd: Path | None = None
        config_path = Path(sys.argv[3]) if len(sys.argv) == 4 else None
        self.config = {
            "mode": "janitor",
            "prompt": PROMPT,
            "cycles": 1,
            "slices": None,
            "meta_plan": None,
            "improvements": 1,
            "review": 1,
        }
        if config_path is not None:
            self.config.update(json.loads(config_path.read_text(encoding="utf-8")))
        if self.config.get("mode") == "builder":
            self.expected_stages = build_expected_builder_stages(
                prompt=self.config.get("prompt"),
                slices=int(self.config["slices"]),
                improvement_count=int(self.config["improvements"]),
                review_count=int(self.config["review"]),
                includes_meta_plan_creation=self.config.get("meta_plan") is None,
            )
        else:
            self.expected_stages = build_expected_refactor_stages(
                self.config.get("prompt"),
                cycles=int(self.config["cycles"]),
                improvement_count=int(self.config["improvements"]),
                review_count=int(self.config["review"]),
            )
        self.error: str | None = None

    def active_link_path(self) -> Path:
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        return self.run_cwd / ".agent" / "active"

    def work_item_path(self) -> Path:
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        if self.config.get("mode") == "builder":
            slice_id = self.active_meta_plan_slice_id() or "slice-1"
            return self.run_cwd / ".agent" / "work" / f"2026-04-21-test-{slice_id}"
        return self.run_cwd / ".agent" / "work" / "2026-04-21-test-refactor"

    def active_meta_plan_path(self) -> Path:
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        return self.run_cwd / ".agent" / "meta-plans" / "2026-04-21-test-builder"

    def active_meta_plan_link_path(self) -> Path:
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        return self.run_cwd / ".agent" / "meta-plans" / "active"

    def active_meta_plan_slice_id(self) -> str | None:
        meta_path = self.active_meta_plan_path() / "meta.json"
        if not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        value = meta.get("active_slice_id")
        return str(value) if value else None

    def work_item_meta_path(self) -> Path:
        return self.work_item_path() / "meta.json"

    def work_item_execplan_path(self) -> Path:
        return self.work_item_path() / "execplan.md"

    def work_item_decision_path(self) -> Path:
        return self.work_item_path() / "decision.md"

    def work_item_candidates_path(self) -> Path:
        return self.work_item_path() / "candidates.md"

    def write_active_link(self) -> None:
        active_path = self.active_link_path()
        active_path.parent.mkdir(parents=True, exist_ok=True)
        if active_path.exists() or active_path.is_symlink():
            active_path.unlink()
        active_path.symlink_to(self.work_item_path())

    def write_meta(self, *, stage: str, state: str) -> None:
        payload = {
            "id": self.work_item_path().name,
            "slug": self.work_item_path().name.removeprefix("2026-04-21-"),
            "title": "Test refactor work item",
            "created_at": "2026-04-21T10:00:00Z",
            "updated_at": "2026-04-21T10:05:00Z",
            "stage": stage,
            "state": state,
            "artifacts": {
                "candidates": "candidates.md",
                "decision": "decision.md",
                "execplan": "execplan.md",
                "review": None,
            },
        }
        if self.config.get("mode") == "builder":
            slice_id = self.active_meta_plan_slice_id() or "slice-1"
            payload["meta_plan_id"] = "2026-04-21-test-builder"
            payload["meta_plan_slice_id"] = slice_id
        self.work_item_meta_path().write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def write_meta_plan(self) -> None:
        meta_plan_path = self.active_meta_plan_path()
        meta_plan_path.mkdir(parents=True, exist_ok=True)
        active_link = self.active_meta_plan_link_path()
        active_link.parent.mkdir(parents=True, exist_ok=True)
        if active_link.exists() or active_link.is_symlink():
            active_link.unlink()
        active_link.symlink_to(meta_plan_path)
        slices = [
            {
                "id": f"slice-{index}",
                "title": f"Slice {index}",
                "summary": f"Build slice {index}",
                "status": "active" if index == 1 else "ready",
                "child_work_item_id": None,
                "child_work_item_path": None,
                "result_summary": None,
                "notes": [],
            }
            for index in range(1, int(self.config["slices"]) + 1)
        ]
        meta = {
            "id": "2026-04-21-test-builder",
            "slug": "test-builder",
            "title": "Test builder",
            "created_at": "2026-04-21T10:00:00Z",
            "updated_at": "2026-04-21T10:00:00Z",
            "status": "active",
            "active_slice_id": "slice-1",
            "artifacts": {"brief": "brief.md", "slices": "slices.json"},
        }
        (meta_plan_path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        (meta_plan_path / "brief.md").write_text("Test builder brief\n", encoding="utf-8")
        (meta_plan_path / "slices.json").write_text(json.dumps(slices, indent=2, sort_keys=True), encoding="utf-8")

    def record_child_work_item_on_active_slice(self) -> None:
        slice_id = self.active_meta_plan_slice_id()
        if slice_id is None:
            return
        slices_path = self.active_meta_plan_path() / "slices.json"
        slices = json.loads(slices_path.read_text(encoding="utf-8"))
        for item in slices:
            if item.get("id") == slice_id:
                item["child_work_item_id"] = self.work_item_path().name
                item["child_work_item_path"] = f".agent/work/{self.work_item_path().name}"
        slices_path.write_text(json.dumps(slices, indent=2, sort_keys=True), encoding="utf-8")

    def reconcile_active_slice(self) -> None:
        slice_id = self.active_meta_plan_slice_id()
        if slice_id is None:
            return
        meta_path = self.active_meta_plan_path() / "meta.json"
        slices_path = self.active_meta_plan_path() / "slices.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        slices = json.loads(slices_path.read_text(encoding="utf-8"))
        next_slice_id: str | None = None
        for index, item in enumerate(slices):
            if item.get("id") != slice_id:
                continue
            item["status"] = "completed"
            item["result_summary"] = f"Completed {slice_id}"
            item["notes"] = [*item.get("notes", []), "reviewed"]
            if index + 1 < len(slices):
                next_slice_id = str(slices[index + 1]["id"])
                slices[index + 1]["status"] = "active"
            break
        if next_slice_id is None:
            meta["status"] = "completed"
        else:
            meta["status"] = "active"
            meta["active_slice_id"] = next_slice_id
        slices_path.write_text(json.dumps(slices, indent=2, sort_keys=True), encoding="utf-8")
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    def linked_studio_repo_path(self) -> Path:
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        return self.run_cwd.parent / "openclaw-studio-private"

    def complete_cycle_plan_side_effect(self, stage_index: int) -> None:
        if self.scenario == "refactor_missing_execplan":
            return
        stage = self.expected_stages[stage_index]
        if stage["skill_name"] == "create-meta-plan":
            self.write_meta_plan()
            return
        work_item_path = self.work_item_path()
        if stage["skill_name"] == "find-refactor-candidates":
            work_item_path.mkdir(parents=True, exist_ok=True)
            self.write_active_link()
            self.write_meta(stage="candidates", state="completed")
            self.work_item_candidates_path().write_text(f"candidates for stage {stage_index + 1}\n", encoding="utf-8")
            return
        if stage["skill_name"] == "select-refactor":
            work_item_path.mkdir(parents=True, exist_ok=True)
            self.write_active_link()
            self.write_meta(stage="decision", state="completed")
            self.work_item_decision_path().write_text(f"decision for stage {stage_index + 1}\n", encoding="utf-8")
            return
        if stage["skill_name"] in {"execplan-create", "execplan-improve"}:
            work_item_path.mkdir(parents=True, exist_ok=True)
            self.write_active_link()
            self.write_meta(stage="plan", state="completed")
            self.work_item_execplan_path().write_text(f"plan for stage {stage_index + 1}\n", encoding="utf-8")
            if self.config.get("mode") == "builder" and stage["skill_name"] == "execplan-create":
                self.record_child_work_item_on_active_slice()
            return
        if stage["skill_name"] == "implement-execplan":
            work_item_path.mkdir(parents=True, exist_ok=True)
            self.write_active_link()
            self.write_meta(stage="implementation", state="completed")
        if self.config.get("mode") == "builder" and stage["skill_name"] == "review-recent-work":
            review_suffix = f"review-recent-work-{self.config['review']}"
            if not str(stage["label"]).endswith(review_suffix):
                return
            self.reconcile_active_slice()
        if self.scenario == "review_mutates_linked_repo" and stage["skill_name"] in {
            "review-recent-work",
        }:
            linked_repo = self.linked_studio_repo_path()
            linked_repo.mkdir(parents=True, exist_ok=True)
            review_note = linked_repo / f"review-{stage_index + 1}.txt"
            review_note.write_text(f"review change for stage {stage_index + 1}\n", encoding="utf-8")

    def handle_thread_start(self) -> None:
        thread_start = self.expect_request("thread/start")
        thread_params = thread_start.get("params", {})
        if thread_params.get("approvalPolicy") != "never":
            raise ProtocolError(f"unexpected approvalPolicy: {thread_params}")
        sandbox_mode = thread_params.get("sandbox")
        if sandbox_mode not in {"workspace-write", "danger-full-access"}:
            raise ProtocolError(f"unexpected sandbox: {thread_params}")
        config = thread_params.get("config")
        if sandbox_mode == "workspace-write":
            if not isinstance(config, dict):
                raise ProtocolError(f"workspace-write missing config: {thread_params}")
            sandbox_workspace_write = config.get("sandbox_workspace_write")
            if not isinstance(sandbox_workspace_write, dict):
                raise ProtocolError(f"workspace-write missing sandbox_workspace_write: {thread_params}")
            if not isinstance(sandbox_workspace_write.get("writable_roots"), list):
                raise ProtocolError(f"workspace-write missing writable_roots: {thread_params}")
        elif config is not None:
            raise ProtocolError(f"unexpected config for sandbox: {thread_params}")
        if not isinstance(thread_params.get("cwd"), str) or not thread_params["cwd"]:
            raise ProtocolError(f"thread/start missing cwd: {thread_params}")
        self.run_cwd = Path(thread_params["cwd"])
        self.thread_count += 1
        self.thread_id = f"thread-{self.thread_count}"
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
                    "sandbox": {"mode": sandbox_mode},
                    "reasoningEffort": "medium",
                },
            }
        )
        self.send({"method": "thread/started", "params": {"thread": {"id": self.thread_id}}})

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
        existing_sessions: list[dict[str, Any]] = []
        if self.record_path.exists():
            existing = json.loads(self.record_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing_sessions = list(existing.get("sessions", []))
        session_record = {
            "serverCwd": os.getcwd(),
            "transcript": self.transcript,
            "error": self.error,
        }
        sessions = [*existing_sessions, session_record]
        transcript: list[dict[str, Any]] = []
        for session in sessions:
            transcript.extend(session.get("transcript", []))
        self.record_path.write_text(
            json.dumps(
                {
                    "scenario": self.scenario,
                    "serverCwd": os.getcwd(),
                    "transcript": transcript,
                    "error": self.error,
                    "sessions": sessions,
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

        self.handle_thread_start()

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
                failure_message="interactive tool input is unsupported in this workflow",
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
                failure_message="MCP elicitation is unsupported in this workflow",
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
                failure_message="permission approval is unsupported in this workflow",
            )
            return
        if self.scenario == "chatgpt_auth_refresh":
            self.run_server_request_stage(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized", "previousAccountId": None},
                expect_error="external auth refresh is unsupported in slop-janitor",
                failure_message="external auth refresh is unsupported in this workflow",
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
        if self.scenario == "retryable_stage_error":
            self.run_retryable_stage_error()
            return
        if self.scenario == "reader_error_then_recover":
            self.run_reader_error_then_recover()
            return
        if self.scenario == "hanging_turn_start_then_recover":
            self.run_hanging_turn_start_then_recover()
            return
        if self.scenario == "retryable_impl_ambiguity":
            self.run_retryable_impl_ambiguity()
            return
        if self.scenario == "approval_request_after_plan_refresh":
            self.run_approval_request_after_plan_refresh()
            return
        if self.scenario == "retryable_impl_postcondition_success":
            self.run_retryable_impl_postcondition_success()
            return
        if self.scenario == "retryable_impl_postcondition_missing_tokens":
            self.run_retryable_impl_postcondition_missing_tokens()
            return
        if self.scenario == "happy_path":
            self.run_happy_path()
            return
        if self.scenario == "goal_run":
            self.run_goal_plan(goal_count=1)
            return
        if self.scenario == "goal_run_two":
            self.run_goal_plan(goal_count=2)
            return
        if self.scenario == "goal_feature_disabled":
            self.run_goal_feature_disabled()
            return
        if self.scenario in {
            "refactor_with_prompt",
            "refactor_without_prompt",
            "refactor_missing_execplan",
            "review_mutates_linked_repo",
        }:
            self.run_happy_path()
            return
        raise ProtocolError(f"unsupported scenario: {self.scenario}")

    def handle_goal_set(self) -> None:
        request = self.expect_request("thread/goal/set")
        params = request.get("params", {})
        if params.get("threadId") != self.thread_id:
            raise ProtocolError(f"thread/goal/set used wrong thread id: {params}")
        objective = params.get("objective")
        if not isinstance(objective, str) or "Goal:" not in objective:
            raise ProtocolError(f"thread/goal/set missing objective: {params}")
        if params.get("status") != "active":
            raise ProtocolError(f"thread/goal/set missing active status: {params}")
        self.current_goal = {
            "threadId": self.thread_id,
            "objective": objective,
            "status": "active",
            "tokenBudget": None,
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
            "createdAt": 1,
            "updatedAt": 1,
        }
        self.send({"id": request["id"], "result": {"goal": self.current_goal}})
        self.send(
            {
                "method": "thread/goal/updated",
                "params": {"threadId": self.thread_id, "turnId": None, "goal": self.current_goal},
            }
        )

    def handle_goal_get(self) -> None:
        request = self.expect_request("thread/goal/get")
        if request.get("params", {}).get("threadId") != self.thread_id:
            raise ProtocolError(f"thread/goal/get used wrong thread id: {request}")
        self.send({"id": request["id"], "result": {"goal": self.current_goal}})

    def handle_goal_feature_disabled(self) -> None:
        request = self.expect_request("thread/goal/get")
        if request.get("params", {}).get("threadId") != self.thread_id:
            raise ProtocolError(f"thread/goal/get used wrong thread id: {request}")
        self.send(
            {
                "id": request["id"],
                "error": {
                    "code": -32602,
                    "message": "goals feature is disabled",
                    "data": None,
                },
            }
        )

    def handle_goal_clear(self) -> None:
        request = self.expect_request("thread/goal/clear")
        if request.get("params", {}).get("threadId") != self.thread_id:
            raise ProtocolError(f"thread/goal/clear used wrong thread id: {request}")
        self.current_goal = None
        self.send({"id": request["id"], "result": {"cleared": True}})
        self.send({"method": "thread/goal/cleared", "params": {"threadId": self.thread_id}})

    def run_goal_plan(self, *, goal_count: int) -> None:
        self.handle_goal_get()
        for index in range(goal_count):
            self.handle_goal_set()
            turn_id = f"goal-turn-{index + 1}"
            turn_start = self.expect_request("turn/start")
            params = turn_start.get("params", {})
            inputs = params.get("input")
            if params.get("threadId") != self.thread_id:
                raise ProtocolError(f"goal turn/start used wrong thread id: {params}")
            if not isinstance(inputs, list) or len(inputs) != 1 or inputs[0].get("type") != "text":
                raise ProtocolError(f"goal turn/start expected one text input: {params}")
            self.send({"id": turn_start["id"], "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            self.send(
                {
                    "method": "turn/started",
                    "params": {"threadId": self.thread_id, "turn": {"id": turn_id, "status": "inProgress"}},
                }
            )
            self.send_token_usage(turn_id, index)
            if self.current_goal is None:
                raise ProtocolError("goal was not set before turn")
            self.current_goal = {**self.current_goal, "status": "complete", "tokensUsed": 100, "timeUsedSeconds": 5}
            self.send(
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": self.thread_id, "turnId": turn_id, "goal": self.current_goal},
                }
            )
            self.complete_turn(turn_id)
            self.handle_goal_get()
            self.handle_goal_clear()

    def run_goal_feature_disabled(self) -> None:
        self.handle_goal_feature_disabled()

    def validate_turn_start(self, message: dict[str, Any], stage_index: int, *, turn_id: str | None = None) -> tuple[int, str]:
        self.check_turn_start_inputs(message, stage_index)
        if turn_id is None:
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

    def complete_successful_stage(self, stage_index: int, turn_id: str) -> None:
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
        self.complete_cycle_plan_side_effect(stage_index)
        self.send_token_usage(turn_id, stage_index)
        self.complete_turn(turn_id)

    def run_happy_path(self, *, start_stage_index: int = 0) -> None:
        cycle_length = workflow_topology.stages_per_cycle(
            improvement_count=int(self.config["improvements"]),
            review_count=int(self.config["review"]),
        )
        builder_slice_length = workflow_topology.builder_stages_per_slice(
            improvement_count=int(self.config["improvements"]),
            review_count=int(self.config["review"]),
        )
        for stage_index in range(start_stage_index, len(self.expected_stages)):
            if self.config.get("mode") == "builder":
                if self.config.get("meta_plan") is None:
                    should_start_thread = stage_index > start_stage_index and stage_index >= 1 and (stage_index - 1) % builder_slice_length == 0
                else:
                    should_start_thread = stage_index > start_stage_index and stage_index % builder_slice_length == 0
            else:
                should_start_thread = stage_index > start_stage_index and stage_index % cycle_length == 0
            if should_start_thread:
                self.handle_thread_start()
            turn_start = self.expect_request("turn/start")
            _, turn_id = self.validate_turn_start(turn_start, stage_index)
            self.complete_successful_stage(stage_index, turn_id)

    def run_retryable_stage_error(self) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0, turn_id="turn-1-failed")
        self.send(
            {
                "method": "error",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "additionalDetails": "serverOverloaded",
                    },
                },
            }
        )
        self.complete_turn(
            turn_id,
            status="failed",
            error={
                "message": "Selected model is at capacity. Please try a different model.",
                "additionalDetails": "serverOverloaded",
            },
        )
        retry_turn_start = self.expect_request("turn/start")
        _, retry_turn_id = self.validate_turn_start(retry_turn_start, 0, turn_id="turn-1-retry")
        self.complete_successful_stage(0, retry_turn_id)
        self.run_happy_path(start_stage_index=1)

    def run_reader_error_then_recover(self) -> None:
        if self.session_index == 0:
            turn_start = self.expect_request("turn/start")
            self.check_turn_start_inputs(turn_start, 0)
            return
        self.run_happy_path()

    def run_hanging_turn_start_then_recover(self) -> None:
        if self.session_index == 0:
            turn_start = self.expect_request("turn/start")
            self.check_turn_start_inputs(turn_start, 0)
            self.write_record()
            time.sleep(0.4)
            return
        self.run_happy_path()

    def run_retryable_impl_ambiguity(self) -> None:
        implementation_stage_index = workflow_topology.implementation_stage_position(
            improvement_count=int(self.config["improvements"])
        ) - 1
        for stage_index in range(implementation_stage_index):
            turn_start = self.expect_request("turn/start")
            _, turn_id = self.validate_turn_start(turn_start, stage_index)
            self.complete_successful_stage(stage_index, turn_id)
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, implementation_stage_index, turn_id="turn-impl-failed")
        if self.run_cwd is None:
            raise ProtocolError("run cwd is not set")
        (self.run_cwd / "partial-implementation.txt").write_text("partial\n", encoding="utf-8")
        self.send(
            {
                "method": "error",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "additionalDetails": "serverOverloaded",
                    },
                },
            }
        )
        self.complete_turn(
            turn_id,
            status="failed",
            error={
                "message": "Selected model is at capacity. Please try a different model.",
                "additionalDetails": "serverOverloaded",
            },
        )

    def run_approval_request_after_plan_refresh(self) -> None:
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, 0)
        self.complete_cycle_plan_side_effect(0)
        self.send_token_usage(turn_id, 0)
        request_id = 900
        self.send(
            {
                "id": request_id,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "itemId": "cmd-1",
                    "command": ["git", "status"],
                },
            }
        )
        response = self.expect_client_result(request_id)
        if response.get("result") != {"decision": "decline"}:
            raise ProtocolError(f"unexpected result for approval request: {response}")
        self.complete_turn(
            turn_id,
            status="failed",
            error={"message": "received unexpected command approval request while approvalPolicy=never"},
        )

    def run_retryable_impl_postcondition_success(self) -> None:
        implementation_stage_index = workflow_topology.implementation_stage_position(
            improvement_count=int(self.config["improvements"])
        ) - 1
        for stage_index in range(implementation_stage_index):
            turn_start = self.expect_request("turn/start")
            _, turn_id = self.validate_turn_start(turn_start, stage_index)
            self.complete_successful_stage(stage_index, turn_id)
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(turn_start, implementation_stage_index, turn_id="turn-impl-postcondition")
        self.complete_cycle_plan_side_effect(implementation_stage_index)
        self.send_token_usage(turn_id, implementation_stage_index)
        self.send(
            {
                "method": "error",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "additionalDetails": "serverOverloaded",
                    },
                },
            }
        )
        self.complete_turn(
            turn_id,
            status="failed",
            error={
                "message": "Selected model is at capacity. Please try a different model.",
                "additionalDetails": "serverOverloaded",
            },
        )
        self.run_happy_path(start_stage_index=implementation_stage_index + 1)

    def run_retryable_impl_postcondition_missing_tokens(self) -> None:
        implementation_stage_index = workflow_topology.implementation_stage_position(
            improvement_count=int(self.config["improvements"])
        ) - 1
        for stage_index in range(implementation_stage_index):
            turn_start = self.expect_request("turn/start")
            _, turn_id = self.validate_turn_start(turn_start, stage_index)
            self.complete_successful_stage(stage_index, turn_id)
        turn_start = self.expect_request("turn/start")
        _, turn_id = self.validate_turn_start(
            turn_start,
            implementation_stage_index,
            turn_id="turn-impl-postcondition-no-tokens",
        )
        self.complete_cycle_plan_side_effect(implementation_stage_index)
        self.send(
            {
                "method": "error",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "additionalDetails": "serverOverloaded",
                    },
                },
            }
        )
        self.complete_turn(
            turn_id,
            status="failed",
            error={
                "message": "Selected model is at capacity. Please try a different model.",
                "additionalDetails": "serverOverloaded",
            },
        )

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

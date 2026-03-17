from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any


if TYPE_CHECKING:
    from codex_refactor_loop.cli import Stage
    from codex_refactor_loop.cli import TokenUsageSnapshot
    from codex_refactor_loop.cli import TokenUsageSummary
    from codex_refactor_loop.run_log import RunLogger


JSONValue = Any

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppServerSpawnSpec:
    argv: tuple[str, ...]
    cwd: str


@dataclass
class TurnResult:
    turn_id: str
    status: str
    assistant_text: str
    token_usage: TokenUsageSummary | None
    error_message: str | None


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, spawn_spec: AppServerSpawnSpec, run_logger: RunLogger) -> None:
        self.spawn_spec = spawn_spec
        self.run_logger = run_logger
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending_events: deque[dict[str, Any]] = deque()
        self._request_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._process is not None:
            return
        LOGGER.info(
            "starting app-server: %s (cwd=%s)",
            " ".join(self.spawn_spec.argv),
            self.spawn_spec.cwd,
        )
        try:
            self._process = subprocess.Popen(
                self.spawn_spec.argv,
                cwd=self.spawn_spec.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            command = " ".join(self.spawn_spec.argv)
            raise AppServerError(
                f"failed to spawn `{command}` in {self.spawn_spec.cwd}: {exc}"
            ) from exc
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-refactor-loop",
                    "title": "codex-refactor-loop",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized"})

    def get_account(self) -> dict[str, Any]:
        return self._request("account/read", {"refreshToken": False})

    def start_thread(self, cwd: str) -> str:
        result = self._request(
            "thread/start",
            {"cwd": cwd, "approvalPolicy": "never"},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start response did not include thread.id")
        return thread["id"]

    def run_turn(self, thread_id: str, stage: Stage) -> TurnResult:
        result = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {"type": "text", "text": stage.text, "textElements": []},
                    {
                        "type": "skill",
                        "name": stage.skill_name,
                        "path": stage.skill_path,
                    },
                ],
            },
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start response did not include turn.id")
        turn_id = turn["id"]
        assistant_parts: dict[str, list[str]] = {}
        assistant_completed: dict[str, str] = {}
        assistant_order: list[str] = []
        token_usage: TokenUsageSummary | None = None
        failure_message: str | None = None

        while True:
            event = self._next_event()
            kind = event["kind"]
            if kind == "reader_error":
                raise AppServerError(event["message"])
            if kind in {"response", "transport_error"}:
                self._pending_events.append(event)
                continue

            message = event["message"]
            if kind == "notification":
                method = message["method"]
                params = message.get("params", {})
                if method == "thread/tokenUsage/updated":
                    if params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                        token_usage = self._parse_token_usage(params.get("tokenUsage"))
                    continue
                if method == "item/agentMessage/delta":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    item_id = params.get("itemId")
                    delta = params.get("delta", "")
                    if isinstance(item_id, str):
                        if item_id not in assistant_parts:
                            assistant_parts[item_id] = []
                            assistant_order.append(item_id)
                        assistant_parts[item_id].append(delta)
                    if isinstance(delta, str):
                        self.run_logger.write(delta, to_terminal=True)
                    continue
                if method == "item/commandExecution/outputDelta":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    delta = params.get("delta", "")
                    if isinstance(delta, str):
                        self.run_logger.write(delta)
                    continue
                if method == "item/fileChange/outputDelta":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    delta = params.get("delta", "")
                    if delta:
                        self.run_logger.write_line(f"[fileChange] {delta}")
                    continue
                if method == "item/mcpToolCall/progress":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    message_text = params.get("message")
                    if message_text:
                        self.run_logger.write_line(f"[mcp] {message_text}")
                    continue
                if method == "item/started":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    item = params.get("item", {})
                    self._register_agent_item(item, assistant_parts, assistant_completed, assistant_order)
                    self.run_logger.write_line(f"[started] {self._describe_item(item)}")
                    continue
                if method == "item/completed":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    item = params.get("item", {})
                    self._register_agent_item(item, assistant_parts, assistant_completed, assistant_order)
                    if item.get("type") == "agentMessage":
                        item_id = item.get("id")
                        if isinstance(item_id, str) and not assistant_parts.get(item_id):
                            text = item.get("text", "")
                            if isinstance(text, str) and text:
                                self.run_logger.write(text, to_terminal=True)
                    self.run_logger.write_line(f"[completed] {self._describe_item(item)}")
                    continue
                if method == "error":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    turn_error = params.get("error", {})
                    if params.get("willRetry"):
                        self.run_logger.write_line(
                            f"[warning] {self._format_turn_error(turn_error)}",
                            stream="stderr",
                        )
                    else:
                        failure_message = self._format_turn_error(turn_error)
                    continue
                if method == "turn/completed":
                    if params.get("threadId") != thread_id:
                        continue
                    completed_turn = params.get("turn", {})
                    if completed_turn.get("id") != turn_id:
                        continue
                    status = str(completed_turn.get("status", "")).lower()
                    completed_error = self._format_turn_error(completed_turn.get("error"))
                    if status != "completed":
                        failure_message = completed_error or failure_message
                    elif failure_message is not None:
                        status = "failed"
                    elif token_usage is None:
                        failure_message = failure_message or "successful turn completed without token usage data"
                        status = "failed"
                    assistant_text = self._assemble_assistant_text(
                        assistant_parts,
                        assistant_completed,
                        assistant_order,
                    )
                    return TurnResult(
                        turn_id=turn_id,
                        status=status,
                        assistant_text=assistant_text,
                        token_usage=token_usage,
                        error_message=failure_message,
                    )
                continue

            if kind == "server_request":
                failure_message = failure_message or self._handle_server_request(message)
                continue

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        LOGGER.info("closing app-server process")
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.stdout is not None and not process.stdout.closed:
            try:
                process.stdout.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
            self._reader_thread = None
        self._process = None

    def _reader_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        stdout = self._process.stdout
        while True:
            line = stdout.readline()
            if line == "":
                self._queue.put({"kind": "reader_error", "message": "app-server stdout closed unexpectedly"})
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._queue.put({"kind": "reader_error", "message": f"failed to decode JSON-RPC message: {exc}"})
                return
            self._queue.put(self._classify_message(message))

    def _classify_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if "method" in message and "id" in message:
            return {"kind": "server_request", "message": message}
        if "method" in message:
            return {"kind": "notification", "message": message}
        if "id" in message and "error" in message:
            return {"kind": "transport_error", "message": message}
        if "id" in message and "result" in message:
            return {"kind": "response", "message": message}
        return {"kind": "reader_error", "message": f"unknown JSON-RPC payload shape: {message}"}

    def _next_request_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerError("app-server process is not running")
        self._process.stdin.write(json.dumps(message))
        self._process.stdin.write("\n")
        self._process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_request_id()
        deferred_events: deque[dict[str, Any]] = deque()
        self._send({"method": method, "id": request_id, "params": params})
        while True:
            event = self._next_event()
            kind = event["kind"]
            if kind == "reader_error":
                raise AppServerError(event["message"])
            if kind == "response":
                message = event["message"]
                if message.get("id") == request_id:
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise AppServerError(f"`{method}` returned a non-object result")
                    self._restore_deferred_events(deferred_events)
                    return result
                deferred_events.append(event)
                continue
            if kind == "transport_error":
                message = event["message"]
                if message.get("id") == request_id:
                    error = message.get("error", {})
                    code = error.get("code")
                    error_message = error.get("message", "unknown JSON-RPC error")
                    self._restore_deferred_events(deferred_events)
                    raise AppServerError(f"`{method}` failed with JSON-RPC error {code}: {error_message}")
                deferred_events.append(event)
                continue
            deferred_events.append(event)

    def _next_event(self) -> dict[str, Any]:
        if self._pending_events:
            return self._pending_events.popleft()
        return self._queue.get()

    def _restore_deferred_events(self, deferred_events: deque[dict[str, Any]]) -> None:
        while deferred_events:
            self._pending_events.appendleft(deferred_events.pop())

    def _send_server_result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def _send_server_error(self, request_id: Any, message: str) -> None:
        LOGGER.warning("rejecting server request %s: %s", request_id, message)
        self._send({"id": request_id, "error": {"code": -32000, "message": message, "data": None}})

    def _handle_server_request(self, request: dict[str, Any]) -> str:
        method = request["method"]
        request_id = request["id"]
        if method == "item/commandExecution/requestApproval":
            LOGGER.warning("declining unexpected command approval request")
            self._send_server_result(request_id, {"decision": "decline"})
            return "received unexpected command approval request while approvalPolicy=never"
        if method == "item/fileChange/requestApproval":
            LOGGER.warning("declining unexpected file change approval request")
            self._send_server_result(request_id, {"decision": "decline"})
            return "received unexpected file change approval request while approvalPolicy=never"
        if method == "item/tool/requestUserInput":
            LOGGER.warning("declining interactive tool input request")
            self._send_server_result(request_id, {"answers": {}})
            return "interactive tool input is unsupported in this pipeline"
        if method == "mcpServer/elicitation/request":
            LOGGER.warning("declining MCP elicitation request")
            self._send_server_result(request_id, {"action": "decline", "content": None, "_meta": None})
            return "MCP elicitation is unsupported in this pipeline"
        if method == "item/permissions/requestApproval":
            LOGGER.warning("declining permissions approval request")
            self._send_server_result(request_id, {"permissions": {}, "scope": "turn"})
            return "permission approval is unsupported in this pipeline"
        if method == "account/chatgptAuthTokens/refresh":
            self._send_server_error(request_id, "external auth refresh is unsupported in codex-refactor-loop")
            return "external auth refresh is unsupported in this pipeline"
        self._send_server_error(request_id, f"unsupported server request `{method}`")
        return f"received unsupported server request `{method}`"

    def _describe_item(self, item: dict[str, Any]) -> str:
        item_type = item.get("type", "unknown")
        if item_type == "commandExecution":
            return f"commandExecution {item.get('command', '')}".strip()
        if item_type == "fileChange":
            changes = item.get("changes") or []
            return f"fileChange {len(changes)} path(s)"
        if item_type == "mcpToolCall":
            server = item.get("server", "")
            tool = item.get("tool", "")
            return f"mcpToolCall {server}.{tool}".strip(".")
        if item_type == "agentMessage":
            phase = item.get("phase")
            return f"agentMessage {phase}".strip()
        return item_type

    def _register_agent_item(
        self,
        item: dict[str, Any],
        assistant_parts: dict[str, list[str]],
        assistant_completed: dict[str, str],
        assistant_order: list[str],
    ) -> None:
        if item.get("type") != "agentMessage":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        if item_id not in assistant_parts:
            assistant_parts[item_id] = []
        if item_id not in assistant_order:
            assistant_order.append(item_id)
        text = item.get("text")
        if isinstance(text, str):
            assistant_completed[item_id] = text

    def _assemble_assistant_text(
        self,
        assistant_parts: dict[str, list[str]],
        assistant_completed: dict[str, str],
        assistant_order: list[str],
    ) -> str:
        pieces: list[str] = []
        for item_id in assistant_order:
            if assistant_parts.get(item_id):
                pieces.append("".join(assistant_parts[item_id]))
            else:
                pieces.append(assistant_completed.get(item_id, ""))
        return "".join(pieces)

    def _parse_token_usage(self, payload: Any) -> TokenUsageSummary | None:
        if not isinstance(payload, dict):
            return None
        total = self._parse_token_snapshot(payload.get("total"))
        last = self._parse_token_snapshot(payload.get("last"))
        if total is None or last is None:
            return None
        from codex_refactor_loop.cli import TokenUsageSummary

        return TokenUsageSummary(last=last, total=total)

    def _parse_token_snapshot(self, payload: Any) -> TokenUsageSnapshot | None:
        if not isinstance(payload, dict):
            return None
        try:
            from codex_refactor_loop.cli import TokenUsageSnapshot

            return TokenUsageSnapshot(
                total_tokens=int(payload["totalTokens"]),
                input_tokens=int(payload["inputTokens"]),
                cached_input_tokens=int(payload["cachedInputTokens"]),
                output_tokens=int(payload["outputTokens"]),
                reasoning_output_tokens=int(payload["reasoningOutputTokens"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _format_turn_error(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        message = payload.get("message")
        if not isinstance(message, str):
            return None
        extras: list[str] = [message]
        info = payload.get("codexErrorInfo")
        if info:
            extras.append(str(info))
        details = payload.get("additionalDetails")
        if details:
            extras.append(str(details))
        return " | ".join(extras)

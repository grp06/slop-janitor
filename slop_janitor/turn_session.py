from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from slop_janitor.models import TokenUsageSnapshot
from slop_janitor.models import TokenUsageSummary
from slop_janitor.models import TurnResult


if TYPE_CHECKING:
    from slop_janitor.run_log import RunLogger


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerRequestReply:
    request_id: Any
    result: dict[str, Any] | None
    error_message: str | None
    failure_message: str


class TurnSession:
    def __init__(self, *, thread_id: str, turn_id: str, run_logger: RunLogger) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.run_logger = run_logger
        self.assistant_parts: dict[str, list[str]] = {}
        self.assistant_completed: dict[str, str] = {}
        self.assistant_order: list[str] = []
        self.token_usage: TokenUsageSummary | None = None
        self.failure_message: str | None = None

    def handle_notification(self, message: dict[str, Any]) -> TurnResult | None:
        method = message["method"]
        params = message.get("params", {})
        if method == "thread/tokenUsage/updated":
            if params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id:
                self.token_usage = self._parse_token_usage(params.get("tokenUsage"))
            return None
        if method == "item/agentMessage/delta":
            if not self._matches_turn(params):
                return None
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if isinstance(item_id, str):
                if item_id not in self.assistant_parts:
                    self.assistant_parts[item_id] = []
                    self.assistant_order.append(item_id)
                self.assistant_parts[item_id].append(delta)
            if isinstance(delta, str):
                self.run_logger.write(delta, to_terminal=True)
            return None
        if method == "item/commandExecution/outputDelta":
            if not self._matches_turn(params):
                return None
            delta = params.get("delta", "")
            if isinstance(delta, str):
                self.run_logger.write(delta)
            return None
        if method == "item/fileChange/outputDelta":
            if not self._matches_turn(params):
                return None
            delta = params.get("delta", "")
            if delta:
                self.run_logger.write_line(f"[fileChange] {delta}")
            return None
        if method == "item/mcpToolCall/progress":
            if not self._matches_turn(params):
                return None
            message_text = params.get("message")
            if message_text:
                self.run_logger.write_line(f"[mcp] {message_text}")
            return None
        if method == "item/started":
            if not self._matches_turn(params):
                return None
            item = params.get("item", {})
            self._register_agent_item(item)
            self.run_logger.write_line(f"[started] {self._describe_item(item)}")
            return None
        if method == "item/completed":
            if not self._matches_turn(params):
                return None
            item = params.get("item", {})
            self._register_agent_item(item)
            if item.get("type") == "agentMessage":
                item_id = item.get("id")
                if isinstance(item_id, str) and not self.assistant_parts.get(item_id):
                    text = item.get("text", "")
                    if isinstance(text, str) and text:
                        self.run_logger.write(text, to_terminal=True)
            self.run_logger.write_line(f"[completed] {self._describe_item(item)}")
            return None
        if method == "error":
            if not self._matches_turn(params):
                return None
            turn_error = params.get("error", {})
            if params.get("willRetry"):
                self.run_logger.write_line(
                    f"[warning] {self._format_turn_error(turn_error)}",
                    stream="stderr",
                )
            else:
                self.failure_message = self._format_turn_error(turn_error)
            return None
        if method == "turn/completed":
            if params.get("threadId") != self.thread_id:
                return None
            completed_turn = params.get("turn", {})
            if completed_turn.get("id") != self.turn_id:
                return None
            return self._build_turn_result(completed_turn)
        return None

    def handle_server_request(self, request: dict[str, Any]) -> ServerRequestReply:
        method = request["method"]
        request_id = request["id"]
        if method == "item/commandExecution/requestApproval":
            LOGGER.warning("declining unexpected command approval request")
            reply = ServerRequestReply(
                request_id=request_id,
                result={"decision": "decline"},
                error_message=None,
                failure_message="received unexpected command approval request while approvalPolicy=never",
            )
        elif method == "item/fileChange/requestApproval":
            LOGGER.warning("declining unexpected file change approval request")
            reply = ServerRequestReply(
                request_id=request_id,
                result={"decision": "decline"},
                error_message=None,
                failure_message="received unexpected file change approval request while approvalPolicy=never",
            )
        elif method == "item/tool/requestUserInput":
            LOGGER.warning("declining interactive tool input request")
            reply = ServerRequestReply(
                request_id=request_id,
                result={"answers": {}},
                error_message=None,
                failure_message="interactive tool input is unsupported in this pipeline",
            )
        elif method == "mcpServer/elicitation/request":
            LOGGER.warning("declining MCP elicitation request")
            reply = ServerRequestReply(
                request_id=request_id,
                result={"action": "decline", "content": None, "_meta": None},
                error_message=None,
                failure_message="MCP elicitation is unsupported in this pipeline",
            )
        elif method == "item/permissions/requestApproval":
            LOGGER.warning("declining permissions approval request")
            reply = ServerRequestReply(
                request_id=request_id,
                result={"permissions": {}, "scope": "turn"},
                error_message=None,
                failure_message="permission approval is unsupported in this pipeline",
            )
        elif method == "account/chatgptAuthTokens/refresh":
            reply = ServerRequestReply(
                request_id=request_id,
                result=None,
                error_message="external auth refresh is unsupported in slop-janitor",
                failure_message="external auth refresh is unsupported in this pipeline",
            )
        else:
            reply = ServerRequestReply(
                request_id=request_id,
                result=None,
                error_message=f"unsupported server request `{method}`",
                failure_message=f"received unsupported server request `{method}`",
            )
        self.failure_message = self.failure_message or reply.failure_message
        return reply

    def _matches_turn(self, params: dict[str, Any]) -> bool:
        return params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id

    def _build_turn_result(self, completed_turn: dict[str, Any]) -> TurnResult:
        status = str(completed_turn.get("status", "")).lower()
        completed_error = self._format_turn_error(completed_turn.get("error"))
        failure_message = self.failure_message
        if status != "completed":
            failure_message = completed_error or failure_message
        elif failure_message is not None:
            status = "failed"
        elif self.token_usage is None:
            failure_message = "successful turn completed without token usage data"
            status = "failed"
        return TurnResult(
            turn_id=self.turn_id,
            status=status,
            assistant_text=self._assemble_assistant_text(),
            token_usage=self.token_usage,
            error_message=failure_message,
        )

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

    def _register_agent_item(self, item: dict[str, Any]) -> None:
        if item.get("type") != "agentMessage":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        if item_id not in self.assistant_parts:
            self.assistant_parts[item_id] = []
        if item_id not in self.assistant_order:
            self.assistant_order.append(item_id)
        text = item.get("text")
        if isinstance(text, str):
            self.assistant_completed[item_id] = text

    def _assemble_assistant_text(self) -> str:
        pieces: list[str] = []
        for item_id in self.assistant_order:
            if self.assistant_parts.get(item_id):
                pieces.append("".join(self.assistant_parts[item_id]))
            else:
                pieces.append(self.assistant_completed.get(item_id, ""))
        return "".join(pieces)

    def _parse_token_usage(self, payload: Any) -> TokenUsageSummary | None:
        if not isinstance(payload, dict):
            return None
        total = self._parse_token_snapshot(payload.get("total"))
        last = self._parse_token_snapshot(payload.get("last"))
        if total is None or last is None:
            return None
        return TokenUsageSummary(last=last, total=total)

    def _parse_token_snapshot(self, payload: Any) -> TokenUsageSnapshot | None:
        if not isinstance(payload, dict):
            return None
        try:
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

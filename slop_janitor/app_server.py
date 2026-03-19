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

from slop_janitor.models import Stage
from slop_janitor.models import TurnResult
from slop_janitor.turn_session import TurnSession


if TYPE_CHECKING:
    from slop_janitor.run_log import RunLogger


JSONValue = Any

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppServerSpawnSpec:
    argv: tuple[str, ...]
    cwd: str

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
                    "name": "slop-janitor",
                    "title": "slop-janitor",
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
        session = TurnSession(thread_id=thread_id, turn_id=turn["id"], run_logger=self.run_logger)

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
                turn_result = session.handle_notification(message)
                if turn_result is not None:
                    return turn_result
                continue

            if kind == "server_request":
                reply = session.handle_server_request(message)
                if reply.error_message is None:
                    self._send_server_result(reply.request_id, reply.result or {})
                else:
                    self._send_server_error(reply.request_id, reply.error_message)
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

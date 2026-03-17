from __future__ import annotations

import re
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"


def _sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return sanitized or "root"


def build_run_log_path(runs_dir: Path, run_cwd: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = _sanitize_name(run_cwd.name or str(run_cwd))
    candidate = runs_dir / f"{prefix}-{timestamp}.log"
    suffix = 2
    while candidate.exists():
        candidate = runs_dir / f"{prefix}-{timestamp}-{suffix}.log"
        suffix += 1
    return candidate


class RunLogger:
    def __init__(self, log_path: Path, *, run_cwd: Path, mode: str, prompt: str | None) -> None:
        self.log_path = log_path
        self._file: TextIO | None = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.log_path.open("w", encoding="utf-8", buffering=1)
        self.write_line(f"startedAt={datetime.now(timezone.utc).isoformat()}")
        self.write_line(f"cwd={run_cwd}")
        self.write_line(f"mode={mode}")
        if prompt is not None:
            self.write_line(f"prompt={prompt}")
        self.write_line("")

    def write(self, text: str, *, to_terminal: bool = False, stream: str = "stdout") -> None:
        if self._file is not None:
            self._file.write(text)
            self._file.flush()
        if to_terminal:
            terminal = sys.stderr if stream == "stderr" else sys.stdout
            terminal.write(text)
            terminal.flush()

    def write_line(self, text: str = "", *, to_terminal: bool = False, stream: str = "stdout") -> None:
        self.write(f"{text}\n", to_terminal=to_terminal, stream=stream)

    def close(self) -> None:
        if self._file is None:
            return
        self.write_line("")
        self.write_line(f"endedAt={datetime.now(timezone.utc).isoformat()}")
        self._file.close()
        self._file = None

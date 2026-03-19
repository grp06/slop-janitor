from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    label: str
    skill_name: str
    skill_path: str
    text: str


@dataclass(frozen=True)
class TokenUsageSnapshot:
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


@dataclass(frozen=True)
class TokenUsageSummary:
    last: TokenUsageSnapshot
    total: TokenUsageSnapshot


@dataclass
class TurnResult:
    turn_id: str
    status: str
    assistant_text: str
    token_usage: TokenUsageSummary | None
    error_message: str | None

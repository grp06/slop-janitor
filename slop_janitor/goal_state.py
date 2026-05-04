from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from slop_janitor.app_server import AppServerError


PLAN_STATUSES = {"active", "blocked", "completed", "abandoned"}
GOAL_STATUSES = {"ready", "active", "blocked", "completed", "failed", "skipped"}
REQUIRED_PLAN_FIELDS = {"id", "title", "status", "goals"}
REQUIRED_GOAL_FIELDS = {
    "id",
    "title",
    "objective",
    "rationale",
    "scope",
    "non_goals",
    "stop_condition",
    "acceptance_criteria",
    "validation",
    "depends_on",
    "status",
    "result_summary",
    "evidence",
    "risks",
    "assumptions",
}


@dataclass(frozen=True)
class GoalPlan:
    path: Path
    payload: dict[str, Any]

    @property
    def goals(self) -> list[dict[str, Any]]:
        return self.payload["goals"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def goals_root(run_cwd: Path) -> Path:
    return run_cwd / ".agent" / "goals"


def active_goal_plan_link_path(run_cwd: Path) -> Path:
    return goals_root(run_cwd) / "active"


def normalize_goal_plan_path(run_cwd: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = run_cwd / candidate
    return candidate.resolve(strict=False)


def resolve_active_goal_plan_path(run_cwd: Path) -> Path:
    active_path = active_goal_plan_link_path(run_cwd)
    if not active_path.exists():
        raise AppServerError(
            f"goal plan path was not provided and `{active_path}` is missing. "
            "Create or approve a goal plan first, or pass `.agent/goals/<id-slug>` explicitly."
        )
    return active_path.resolve(strict=False)


def resolve_goal_plan_path(run_cwd: Path, raw_path: str | None) -> Path:
    if raw_path is None:
        return resolve_active_goal_plan_path(run_cwd)
    return normalize_goal_plan_path(run_cwd, raw_path)


def load_goal_plan(run_cwd: Path, raw_path: str | None) -> GoalPlan:
    plan_dir = resolve_goal_plan_path(run_cwd, raw_path)
    if not plan_dir.exists():
        raise AppServerError(f"goal plan does not exist: {plan_dir}")
    if not plan_dir.is_dir():
        raise AppServerError(f"goal plan path is not a directory: {plan_dir}")
    try:
        plan_dir.relative_to(goals_root(run_cwd).resolve(strict=False))
    except ValueError as exc:
        raise AppServerError(f"goal plan must be under `{goals_root(run_cwd)}`: {plan_dir}") from exc
    if not (plan_dir / "brief.md").is_file():
        raise AppServerError(f"goal plan must contain `brief.md`: {plan_dir}")
    if not (plan_dir / "ledger.jsonl").is_file():
        raise AppServerError(f"goal plan must contain `ledger.jsonl`: {plan_dir}")
    goals_path = plan_dir / "goals.json"
    try:
        payload = json.loads(goals_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AppServerError(f"goal plan must contain `goals.json`: {plan_dir}") from exc
    except json.JSONDecodeError as exc:
        raise AppServerError(f"goal plan has invalid JSON in `{goals_path}`: {exc}") from exc
    if not isinstance(payload, dict):
        raise AppServerError(f"`{goals_path}` must contain a JSON object")
    validate_goal_plan_payload(payload, plan_dir=plan_dir)
    return GoalPlan(path=plan_dir, payload=payload)


def validate_goal_plan_payload(payload: dict[str, Any], *, plan_dir: Path | None = None) -> None:
    missing = sorted(REQUIRED_PLAN_FIELDS - payload.keys())
    if missing:
        raise AppServerError(f"goals.json is missing required field(s): {', '.join(missing)}")
    if not isinstance(payload.get("id"), str) or not payload["id"]:
        raise AppServerError("goals.json field `id` must be a non-empty string")
    if not isinstance(payload.get("title"), str) or not payload["title"]:
        raise AppServerError("goals.json field `title` must be a non-empty string")
    status = payload.get("status")
    if status not in PLAN_STATUSES:
        raise AppServerError(f"goals.json field `status` must be one of {sorted(PLAN_STATUSES)}, found `{status}`")
    goals = payload.get("goals")
    if not isinstance(goals, list) or not goals:
        raise AppServerError("goals.json field `goals` must be a non-empty list")
    seen: set[str] = set()
    for index, goal in enumerate(goals, start=1):
        validate_goal_item(goal, index=index, seen=seen)
    active_goal_id = payload.get("active_goal_id")
    if active_goal_id is not None and active_goal_id not in seen:
        raise AppServerError(f"goals.json active_goal_id does not match any goal: {active_goal_id}")
    if plan_dir is not None and payload["id"] != plan_dir.name:
        raise AppServerError(f"goals.json id `{payload['id']}` must match plan directory `{plan_dir.name}`")


def validate_goal_item(goal: Any, *, index: int, seen: set[str]) -> None:
    if not isinstance(goal, dict):
        raise AppServerError(f"goal #{index} must be a JSON object")
    missing = sorted(REQUIRED_GOAL_FIELDS - goal.keys())
    if missing:
        raise AppServerError(f"goal #{index} is missing required field(s): {', '.join(missing)}")
    goal_id = goal.get("id")
    if not isinstance(goal_id, str) or not goal_id:
        raise AppServerError(f"goal #{index} field `id` must be a non-empty string")
    if goal_id in seen:
        raise AppServerError(f"duplicate goal id: {goal_id}")
    seen.add(goal_id)
    for field in ("title", "objective", "rationale", "stop_condition"):
        if not isinstance(goal.get(field), str) or not goal[field]:
            raise AppServerError(f"goal `{goal_id}` field `{field}` must be a non-empty string")
    for field in (
        "scope",
        "non_goals",
        "acceptance_criteria",
        "validation",
        "depends_on",
        "evidence",
        "risks",
        "assumptions",
    ):
        if not isinstance(goal.get(field), list):
            raise AppServerError(f"goal `{goal_id}` field `{field}` must be a list")
    status = goal.get("status")
    if status not in GOAL_STATUSES:
        raise AppServerError(f"goal `{goal_id}` status must be one of {sorted(GOAL_STATUSES)}, found `{status}`")


def select_next_goal(plan: GoalPlan) -> dict[str, Any] | None:
    completed = {goal["id"] for goal in plan.goals if goal.get("status") == "completed"}
    active_goal_id = plan.payload.get("active_goal_id")
    for goal in plan.goals:
        if goal.get("id") == active_goal_id and goal.get("status") in {"active", "ready"}:
            ensure_dependencies_satisfied(goal, completed)
            return goal
    for goal in plan.goals:
        if goal.get("status") != "ready":
            continue
        ensure_dependencies_satisfied(goal, completed)
        return goal
    return None


def ensure_dependencies_satisfied(goal: dict[str, Any], completed: set[str]) -> None:
    missing = [dependency for dependency in goal.get("depends_on", []) if dependency not in completed]
    if missing:
        raise AppServerError(
            f"goal `{goal.get('id')}` is ready but depends on incomplete goal(s): {', '.join(missing)}"
        )


def goal_objective_text(plan: GoalPlan, goal: dict[str, Any]) -> str:
    parts = [
        f"Goal plan: {plan.payload['title']} ({plan.payload['id']})",
        f"Goal: {goal['title']} ({goal['id']})",
        "",
        goal["objective"],
        "",
        f"Stop condition: {goal['stop_condition']}",
    ]
    if goal.get("acceptance_criteria"):
        parts.extend(["", "Acceptance criteria:", *[f"- {item}" for item in goal["acceptance_criteria"]]])
    if goal.get("validation"):
        parts.extend(["", "Validation:", *[f"- {item}" for item in goal["validation"]]])
    return "\n".join(parts)


def mark_goal_started(plan: GoalPlan, goal_id: str, *, thread_id: str) -> None:
    payload = plan.payload
    payload["status"] = "active"
    payload["active_goal_id"] = goal_id
    payload["updated_at"] = now_iso()
    goal = find_goal(payload, goal_id)
    goal["status"] = "active"
    goal["started_at"] = now_iso()
    goal["thread_id"] = thread_id
    write_goal_plan(plan)
    append_ledger(plan.path, {"event": "goal_started", "goal_id": goal_id, "thread_id": thread_id})


def mark_goal_completed(
    plan: GoalPlan,
    goal_id: str,
    *,
    result_summary: str,
    evidence: list[dict[str, Any]],
) -> None:
    payload = plan.payload
    goal = find_goal(payload, goal_id)
    goal["status"] = "completed"
    goal["completed_at"] = now_iso()
    goal["result_summary"] = result_summary
    goal["evidence"] = [*goal.get("evidence", []), *evidence]
    payload["updated_at"] = now_iso()
    next_goal = next((item for item in payload["goals"] if item.get("status") == "ready"), None)
    payload["active_goal_id"] = next_goal["id"] if next_goal is not None else None
    if next_goal is None and all(item.get("status") == "completed" for item in payload["goals"]):
        payload["status"] = "completed"
    write_goal_plan(plan)
    append_ledger(plan.path, {"event": "goal_completed", "goal_id": goal_id, "result_summary": result_summary})


def mark_goal_failed(plan: GoalPlan, goal_id: str, *, error: str) -> None:
    payload = plan.payload
    goal = find_goal(payload, goal_id)
    goal["status"] = "failed"
    goal["result_summary"] = error
    payload["status"] = "blocked"
    payload["active_goal_id"] = goal_id
    payload["updated_at"] = now_iso()
    write_goal_plan(plan)
    append_ledger(plan.path, {"event": "goal_failed", "goal_id": goal_id, "error": error})


def find_goal(payload: dict[str, Any], goal_id: str) -> dict[str, Any]:
    for goal in payload["goals"]:
        if goal.get("id") == goal_id:
            return goal
    raise AppServerError(f"unknown goal id: {goal_id}")


def write_goal_plan(plan: GoalPlan) -> None:
    (plan.path / "goals.json").write_text(
        json.dumps(plan.payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_ledger(plan_dir: Path, event: dict[str, Any]) -> None:
    payload = {"timestamp": now_iso(), **event}
    with (plan_dir / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")

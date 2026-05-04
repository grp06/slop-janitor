from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable

from slop_janitor.app_server import AppServerError
from slop_janitor.models import AutoCommitState
from slop_janitor.models import Stage


@dataclass(frozen=True)
class WorkflowArtifactSnapshot:
    path: str | None
    fingerprint: "FileFingerprint"


@dataclass(frozen=True)
class FileFingerprint:
    exists: bool
    size: int
    sha256: str | None


@dataclass(frozen=True)
class RepoStateSnapshot:
    repo_root: Path
    head_commit: str | None
    status_lines: tuple[str, ...]


@dataclass(frozen=True)
class StageWorkspaceSnapshot:
    repo_states: tuple[RepoStateSnapshot, ...]
    tracked_artifacts: tuple[WorkflowArtifactSnapshot, ...]


GitRepoRootFn = Callable[[Path], Path | None]
GitStatusLinesFn = Callable[[Path, tuple[str, ...]], list[str] | None]
GitHeadCommitFn = Callable[[Path], str | None]
IMPROVE_STAGE_SKILLS = (
    "execplan-improve",
)


def pending_execplan_path(run_cwd: Path) -> Path:
    return run_cwd / ".agent" / "execplan-pending.md"


def agent_dir(run_cwd: Path) -> Path:
    return run_cwd / ".agent"


def active_work_item_link_path(run_cwd: Path) -> Path:
    return agent_dir(run_cwd) / "active"


def meta_plans_dir(run_cwd: Path) -> Path:
    return agent_dir(run_cwd) / "meta-plans"


def active_meta_plan_link_path(run_cwd: Path) -> Path:
    return meta_plans_dir(run_cwd) / "active"


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_active_work_item_dir(run_cwd: Path) -> Path | None:
    active_link = active_work_item_link_path(run_cwd)
    if not active_link.exists() and not active_link.is_symlink():
        return None
    resolved = active_link.resolve(strict=False)
    if resolved.exists() and resolved.is_dir():
        return resolved
    if active_link.is_dir():
        return active_link.resolve(strict=False)
    return None


def resolve_active_meta_plan_dir(run_cwd: Path) -> Path | None:
    active_link = active_meta_plan_link_path(run_cwd)
    if not active_link.exists() and not active_link.is_symlink():
        return None
    resolved = active_link.resolve(strict=False)
    if resolved.exists() and resolved.is_dir():
        return resolved
    if active_link.is_dir():
        return active_link.resolve(strict=False)
    return None


def work_item_artifact_path(work_item_dir: Path, artifact_key: str, default_name: str) -> Path:
    meta = read_json_object(work_item_dir / "meta.json") or {}
    artifacts = meta.get("artifacts")
    if isinstance(artifacts, dict):
        value = artifacts.get(artifact_key)
        if isinstance(value, str) and value:
            return work_item_dir / value
    return work_item_dir / default_name


def active_work_item_artifact_path(run_cwd: Path, artifact_key: str, default_name: str) -> Path | None:
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is None:
        return None
    return work_item_artifact_path(work_item_dir, artifact_key, default_name)


def workflow_tracking_paths(run_cwd: Path) -> tuple[Path, ...]:
    paths: list[Path] = [
        active_work_item_link_path(run_cwd),
        pending_execplan_path(run_cwd),
        active_meta_plan_link_path(run_cwd),
    ]
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is not None:
        paths.extend(
            [
                work_item_dir / "meta.json",
                work_item_artifact_path(work_item_dir, "candidates", "candidates.md"),
                work_item_artifact_path(work_item_dir, "decision", "decision.md"),
                work_item_artifact_path(work_item_dir, "execplan", "execplan.md"),
            ]
        )
    meta_plan_dir_path = resolve_active_meta_plan_dir(run_cwd)
    if meta_plan_dir_path is not None:
        paths.extend(
            [
                meta_plan_dir_path / "meta.json",
                meta_plan_dir_path / "brief.md",
                meta_plan_dir_path / "slices.json",
            ]
        )
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return tuple(deduped)


def relative_path_from_repo(repo_root: Path, path: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(repo_root.resolve(strict=False)).as_posix()
    except ValueError:
        return None


def combine_excluded_relative_paths(*groups: tuple[str, ...]) -> tuple[str, ...]:
    combined: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path in seen:
                continue
            seen.add(path)
            combined.append(path)
    return tuple(combined)


def preferred_execplan_path(run_cwd: Path) -> Path:
    active_execplan = active_work_item_artifact_path(run_cwd, "execplan", "execplan.md")
    if active_execplan is not None:
        return active_execplan
    return pending_execplan_path(run_cwd)


def ensure_execplan_exists(run_cwd: Path, stage: Stage) -> None:
    path = preferred_execplan_path(run_cwd)
    if path.is_file():
        return
    raise AppServerError(f"stage `{stage.label}` requires an execplan, but `{path}` is missing")


def stage_primary_artifact_path(
    run_cwd: Path,
    stage: Stage,
) -> Path | None:
    if stage.skill_name == "create-meta-plan":
        return active_meta_plan_link_path(run_cwd)
    if stage.skill_name == "find-refactor-candidates":
        return active_work_item_artifact_path(run_cwd, "candidates", "candidates.md")
    if stage.skill_name == "select-refactor":
        return active_work_item_artifact_path(run_cwd, "decision", "decision.md")
    if stage.skill_name in {"execplan-create", *IMPROVE_STAGE_SKILLS}:
        return preferred_execplan_path(run_cwd)
    if stage.skill_name == "implement-execplan":
        work_item_dir = resolve_active_work_item_dir(run_cwd)
        if work_item_dir is not None:
            return work_item_dir / "meta.json"
        return pending_execplan_path(run_cwd)
    return None


def fingerprint_path(path: Path) -> FileFingerprint:
    if path.is_symlink():
        target = os.readlink(path)
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return FileFingerprint(exists=True, size=len(target), sha256=digest)
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                digest.update(chunk)
        return FileFingerprint(exists=True, size=path.stat().st_size, sha256=digest.hexdigest())
    if path.exists():
        return FileFingerprint(exists=True, size=0, sha256="dir")
    return FileFingerprint(exists=False, size=0, sha256=None)


def stage_primary_artifact_snapshot(
    run_cwd: Path,
    stage: Stage,
) -> WorkflowArtifactSnapshot:
    path = stage_primary_artifact_path(run_cwd, stage)
    return WorkflowArtifactSnapshot(
        path=str(path) if path is not None else None,
        fingerprint=fingerprint_path(path) if path is not None else FileFingerprint(exists=False, size=0, sha256=None),
    )


def ensure_cycle_start_artifact_was_refreshed(
    run_cwd: Path,
    stage: Stage,
    *,
    previous_snapshot: WorkflowArtifactSnapshot,
) -> None:
    current_snapshot = stage_primary_artifact_snapshot(run_cwd, stage)
    if current_snapshot.path is None or not current_snapshot.fingerprint.exists:
        missing_path = current_snapshot.path or "<unknown artifact>"
        raise AppServerError(f"stage `{stage.label}` did not produce `{missing_path}`")
    if current_snapshot == previous_snapshot:
        raise AppServerError(
            f"stage `{stage.label}` did not refresh `{current_snapshot.path}` for the new cycle"
        )


def implementation_state_completed(run_cwd: Path) -> bool:
    work_item_dir = resolve_active_work_item_dir(run_cwd)
    if work_item_dir is not None:
        meta = read_json_object(work_item_dir / "meta.json") or {}
        return meta.get("stage") == "implementation" and meta.get("state") == "completed"
    return not pending_execplan_path(run_cwd).exists()


def read_slices(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("slices"), list):
        return [item for item in payload["slices"] if isinstance(item, dict)]
    return None


def normalize_meta_plan_path(run_cwd: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = run_cwd / candidate
    if candidate == active_meta_plan_link_path(run_cwd):
        resolved_active = resolve_active_meta_plan_dir(run_cwd)
        if resolved_active is None:
            raise AppServerError(f"`--meta-plan` points at invalid active link: {candidate}")
        return resolved_active
    return candidate.resolve(strict=False)


def validate_existing_meta_plan_path(run_cwd: Path, raw_path: str) -> Path:
    meta_plan_path = normalize_meta_plan_path(run_cwd, raw_path)
    if not meta_plan_path.exists():
        raise AppServerError(f"`--meta-plan` does not exist: {meta_plan_path}")
    if not meta_plan_path.is_dir():
        raise AppServerError(f"`--meta-plan` is not a directory: {meta_plan_path}")
    try:
        meta_plan_path.relative_to(meta_plans_dir(run_cwd).resolve(strict=False))
    except ValueError as exc:
        raise AppServerError(f"`--meta-plan` must be under `{meta_plans_dir(run_cwd)}`: {meta_plan_path}") from exc
    meta = read_json_object(meta_plan_path / "meta.json")
    slices = read_slices(meta_plan_path / "slices.json")
    if meta is None or slices is None:
        raise AppServerError(f"`--meta-plan` must contain readable `meta.json` and `slices.json`: {meta_plan_path}")
    if not (meta_plan_path / "brief.md").is_file():
        raise AppServerError(f"`--meta-plan` must contain `brief.md`: {meta_plan_path}")
    if meta.get("status") != "active":
        raise AppServerError(f"`--meta-plan` status must be `active`, found `{meta.get('status')}`")
    active_slice_id = meta.get("active_slice_id")
    active_slice = next((item for item in slices if item.get("id") == active_slice_id), None)
    if active_slice is None:
        raise AppServerError("`--meta-plan` active_slice_id does not match any slice")
    if active_slice.get("status") not in {"active", "ready"}:
        raise AppServerError(
            f"`--meta-plan` active slice `{active_slice_id}` is `{active_slice.get('status')}`"
        )
    return meta_plan_path


def remaining_slice_count_from_meta_plan(meta_plan_path: Path) -> int:
    meta = read_json_object(meta_plan_path / "meta.json")
    slices = read_slices(meta_plan_path / "slices.json")
    if meta is None or slices is None:
        raise AppServerError(f"could not read existing meta-plan: {meta_plan_path}")
    active_slice_id = meta.get("active_slice_id")
    active_index = next((index for index, item in enumerate(slices) if item.get("id") == active_slice_id), None)
    if active_index is None:
        raise AppServerError("existing meta-plan active_slice_id does not match any slice")
    remaining = sum(1 for item in slices[active_index:] if item.get("status") != "completed")
    if remaining < 1:
        raise AppServerError("existing meta-plan has no remaining slices to execute")
    return remaining


def activate_existing_meta_plan(run_cwd: Path, meta_plan_path: Path) -> None:
    active_link = active_meta_plan_link_path(run_cwd)
    if active_link.exists() or active_link.is_symlink():
        resolved_active = active_link.resolve(strict=False)
        if resolved_active == meta_plan_path.resolve(strict=False):
            return
        active_link.unlink()
    active_link.parent.mkdir(parents=True, exist_ok=True)
    active_link.symlink_to(meta_plan_path)


def active_meta_plan_state(run_cwd: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]] | None:
    meta_plan_dir_path = resolve_active_meta_plan_dir(run_cwd)
    if meta_plan_dir_path is None:
        return None
    meta = read_json_object(meta_plan_dir_path / "meta.json")
    slices = read_slices(meta_plan_dir_path / "slices.json")
    if meta is None or slices is None:
        return None
    return meta_plan_dir_path, meta, slices


def active_meta_plan_signature(run_cwd: Path) -> tuple[str | None, str | None, tuple[tuple[str | None, str | None], ...]]:
    state = active_meta_plan_state(run_cwd)
    if state is None:
        return (None, None, ())
    _, meta, slices = state
    return (
        str(meta.get("status")) if meta.get("status") is not None else None,
        str(meta.get("active_slice_id")) if meta.get("active_slice_id") is not None else None,
        tuple(
            (
                str(item.get("id")) if item.get("id") is not None else None,
                str(item.get("status")) if item.get("status") is not None else None,
            )
            for item in slices
        ),
    )


def ensure_meta_plan_created(run_cwd: Path, *, expected_slices: int) -> None:
    state = active_meta_plan_state(run_cwd)
    if state is None:
        raise AppServerError(
            "stage `create-meta-plan` did not create `.agent/meta-plans/active`, `meta.json`, and `slices.json`"
        )
    meta_plan_dir_path, meta, slices = state
    if not (meta_plan_dir_path / "brief.md").is_file():
        raise AppServerError(f"stage `create-meta-plan` did not create `{meta_plan_dir_path / 'brief.md'}`")
    if len(slices) != expected_slices:
        raise AppServerError(
            f"stage `create-meta-plan` created {len(slices)} slice(s), expected {expected_slices}"
        )
    if meta.get("status") != "active":
        raise AppServerError("stage `create-meta-plan` did not leave the parent meta-plan active")
    active_slice_id = meta.get("active_slice_id")
    if not active_slice_id or not any(item.get("id") == active_slice_id for item in slices):
        raise AppServerError("stage `create-meta-plan` did not set a valid active_slice_id")


def ensure_meta_plan_ready_for_slice(
    run_cwd: Path,
    *,
    slice_index: int,
) -> tuple[str | None, str | None, tuple[tuple[str | None, str | None], ...]]:
    state = active_meta_plan_state(run_cwd)
    if state is None:
        raise AppServerError(f"slice {slice_index} cannot start because `.agent/meta-plans/active` is missing or invalid")
    _, meta, slices = state
    status = meta.get("status")
    if status == "completed":
        raise AppServerError(f"slice {slice_index} cannot start because the parent meta-plan is already completed")
    if status != "active":
        raise AppServerError(f"slice {slice_index} cannot start because the parent meta-plan status is `{status}`")
    active_slice_id = meta.get("active_slice_id")
    active_slice = next((item for item in slices if item.get("id") == active_slice_id), None)
    if active_slice is None:
        raise AppServerError(f"slice {slice_index} cannot start because active_slice_id is invalid")
    if active_slice.get("status") not in {"active", "ready"}:
        raise AppServerError(
            f"slice {slice_index} cannot start because active slice `{active_slice_id}` is `{active_slice.get('status')}`"
        )
    return active_meta_plan_signature(run_cwd)


def ensure_meta_plan_reconciled_after_review(
    run_cwd: Path,
    *,
    slice_index: int,
    previous_signature: tuple[str | None, str | None, tuple[tuple[str | None, str | None], ...]] | None,
) -> tuple[str | None, str | None, tuple[tuple[str | None, str | None], ...]]:
    state = active_meta_plan_state(run_cwd)
    if state is None:
        raise AppServerError(f"slice {slice_index} review did not leave a readable parent meta-plan")
    _, meta, slices = state
    status = meta.get("status")
    if status not in {"active", "blocked", "completed"}:
        raise AppServerError(f"slice {slice_index} review left invalid parent meta-plan status `{status}`")
    if status in {"active", "blocked"}:
        active_slice_id = meta.get("active_slice_id")
        if not active_slice_id or not any(item.get("id") == active_slice_id for item in slices):
            raise AppServerError(f"slice {slice_index} review did not leave a valid active_slice_id")
    current_signature = active_meta_plan_signature(run_cwd)
    if previous_signature is not None and current_signature == previous_signature:
        raise AppServerError(f"slice {slice_index} review did not advance, block, or complete the parent meta-plan")
    return current_signature


def meta_plan_completed(run_cwd: Path) -> bool:
    state = active_meta_plan_state(run_cwd)
    if state is None:
        return False
    _, meta, _ = state
    return meta.get("status") == "completed"


def ensure_implementation_completed(
    run_cwd: Path,
    stage: Stage,
) -> None:
    if implementation_state_completed(run_cwd):
        return
    path = stage_primary_artifact_path(run_cwd, stage) or preferred_execplan_path(run_cwd)
    raise AppServerError(
        f"stage `{stage.label}` completed but did not mark implementation as completed: `{path}`"
    )


def allowed_dirty_paths_for_stage(
    repo_root: Path,
    run_cwd: Path,
    stage: Stage,
    *,
    phase: str,
    git_repo_root_fn: GitRepoRootFn,
) -> tuple[str, ...]:
    primary_repo_root = git_repo_root_fn(run_cwd)
    if primary_repo_root is None:
        return ()
    if repo_root.resolve(strict=False) != primary_repo_root.resolve(strict=False):
        return ()
    tracked_relative_paths = tuple(
        relative_path
        for relative_path in (
            relative_path_from_repo(repo_root, path) for path in workflow_tracking_paths(run_cwd)
        )
        if relative_path is not None
    )
    tracked_relative_paths = combine_excluded_relative_paths(tracked_relative_paths, (".agent",))
    if phase == "start" and stage.skill_name in {
        "select-refactor",
        "execplan-create",
        *IMPROVE_STAGE_SKILLS,
        "implement-execplan",
        "review-recent-work",
    }:
        return tracked_relative_paths
    if phase == "end" and stage.skill_name in {
        "create-meta-plan",
        "find-refactor-candidates",
        "select-refactor",
        "execplan-create",
        *IMPROVE_STAGE_SKILLS,
        "review-recent-work",
    }:
        return tracked_relative_paths
    return ()


def capture_stage_workspace_snapshot(
    auto_commits: list[AutoCommitState],
    run_cwd: Path,
    stage: Stage,
    *,
    git_status_lines_fn: GitStatusLinesFn,
    git_head_commit_fn: GitHeadCommitFn,
    git_repo_root_fn: GitRepoRootFn,
) -> StageWorkspaceSnapshot:
    repo_states: list[RepoStateSnapshot] = []
    for auto_commit in auto_commits:
        if not auto_commit.enabled:
            continue
        excluded_relative_paths = combine_excluded_relative_paths(
            auto_commit.excluded_relative_paths,
            allowed_dirty_paths_for_stage(
                auto_commit.repo_root,
                run_cwd,
                stage,
                phase="start",
                git_repo_root_fn=git_repo_root_fn,
            ),
        )
        status_lines = git_status_lines_fn(auto_commit.repo_root, excluded_relative_paths)
        if status_lines is None:
            raise AppServerError(
                f"stage `{stage.label}` could not inspect git status for auto-managed repo `{auto_commit.repo_root}`"
            )
        repo_states.append(
            RepoStateSnapshot(
                repo_root=auto_commit.repo_root,
                head_commit=git_head_commit_fn(auto_commit.repo_root),
                status_lines=tuple(status_lines),
            )
        )
    return StageWorkspaceSnapshot(
        repo_states=tuple(repo_states),
        tracked_artifacts=tuple(
            WorkflowArtifactSnapshot(path=str(path), fingerprint=fingerprint_path(path))
            for path in workflow_tracking_paths(run_cwd)
        ),
    )


def stage_workspace_matches(
    snapshot: StageWorkspaceSnapshot,
    *,
    auto_commits: list[AutoCommitState],
    run_cwd: Path,
    stage: Stage,
    git_status_lines_fn: GitStatusLinesFn,
    git_head_commit_fn: GitHeadCommitFn,
    git_repo_root_fn: GitRepoRootFn,
) -> bool:
    current = capture_stage_workspace_snapshot(
        auto_commits,
        run_cwd,
        stage,
        git_status_lines_fn=git_status_lines_fn,
        git_head_commit_fn=git_head_commit_fn,
        git_repo_root_fn=git_repo_root_fn,
    )
    return current == snapshot


def serialize_workspace_snapshot(snapshot: StageWorkspaceSnapshot) -> dict[str, Any]:
    return {
        "trackedArtifacts": [
            {
                "path": artifact.path,
                "exists": artifact.fingerprint.exists,
                "size": artifact.fingerprint.size,
                "sha256": artifact.fingerprint.sha256,
            }
            for artifact in snapshot.tracked_artifacts
        ],
        "repos": [
            {
                "repoRoot": str(repo_state.repo_root),
                "headCommit": repo_state.head_commit,
                "statusLines": list(repo_state.status_lines),
            }
            for repo_state in snapshot.repo_states
        ],
    }

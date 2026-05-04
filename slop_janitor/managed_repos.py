from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from slop_janitor.app_server import AppServerError
from slop_janitor.models import AutoCommitState
from slop_janitor.run_log import RunLogger


GitBinaryAvailableFn = Callable[[], bool]
GitRepoRootFn = Callable[[Path], Path | None]
GitStatusHasChangesFn = Callable[[Path, tuple[str, ...]], bool | None]
GitAddAllFn = Callable[[Path, tuple[str, ...]], subprocess.CompletedProcess[str]]
GitCommitFn = Callable[[Path, str], subprocess.CompletedProcess[str]]
GitHasUpstreamFn = Callable[[Path], bool]
GitPushFn = Callable[[Path], subprocess.CompletedProcess[str]]


def git_binary_available() -> bool:
    return shutil.which("git") is not None


def build_auto_commit_state(
    path: Path,
    run_logger: RunLogger,
    *,
    label: str,
    git_binary_available_fn: GitBinaryAvailableFn,
    git_repo_root_fn: GitRepoRootFn,
    git_status_has_changes_fn: GitStatusHasChangesFn,
) -> AutoCommitState:
    if not git_binary_available_fn():
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: `git` is not available")
        return AutoCommitState(enabled=False, repo_root=path)
    repo_root = git_repo_root_fn(path)
    if repo_root is None:
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: target directory is not inside a git repository")
        return AutoCommitState(enabled=False, repo_root=path)
    repo_root_resolved = repo_root.resolve(strict=False)
    excluded_relative_paths: tuple[str, ...] = ()
    try:
        log_relative_path = run_logger.log_path.resolve(strict=False).relative_to(repo_root_resolved)
        excluded_relative_paths = (log_relative_path.as_posix(),)
    except ValueError:
        excluded_relative_paths = ()
    has_changes = git_status_has_changes_fn(repo_root, excluded_relative_paths)
    if has_changes is None:
        run_logger.write_line(f"[commit] auto-commit disabled for {label}: failed to inspect git status")
        return AutoCommitState(enabled=False, repo_root=repo_root)
    if has_changes:
        raise AppServerError(
            f"refusing to start: {label} `{repo_root}` has pre-existing changes. "
            "Commit, stash, or discard them before running slop-janitor."
        )
    run_logger.write_line(f"[commit] auto-commit enabled for {label}: {repo_root}")
    return AutoCommitState(
        enabled=True,
        repo_root=repo_root,
        excluded_relative_paths=excluded_relative_paths,
    )


def extract_repo_paths_from_prompt(prompt: str | None) -> list[Path]:
    if not prompt:
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for match in re.findall(r"(?:~|/)[^\s\"']+", prompt):
        raw_path = match.rstrip("`.,:;!?)]}\"'")
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_dir():
            continue
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


def prepare_auto_commit_state(
    run_cwd: Path,
    run_logger: RunLogger,
    *,
    git_binary_available_fn: GitBinaryAvailableFn,
    git_repo_root_fn: GitRepoRootFn,
    git_status_has_changes_fn: GitStatusHasChangesFn,
) -> AutoCommitState:
    return build_auto_commit_state(
        run_cwd,
        run_logger,
        label="primary repo",
        git_binary_available_fn=git_binary_available_fn,
        git_repo_root_fn=git_repo_root_fn,
        git_status_has_changes_fn=git_status_has_changes_fn,
    )


def resolve_explicit_linked_repo_roots(
    linked_repo_paths: list[str],
    *,
    git_repo_root_fn: GitRepoRootFn,
) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    for raw_path in linked_repo_paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.exists():
            raise AppServerError(f"linked repo path does not exist: {candidate}")
        if not candidate.is_dir():
            raise AppServerError(f"linked repo path is not a directory: {candidate}")
        repo_root = git_repo_root_fn(candidate)
        if repo_root is None:
            raise AppServerError(f"linked repo path is not inside a git repository: {candidate}")
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def resolve_prompt_linked_repo_roots(
    prompt: str | None,
    *,
    git_repo_root_fn: GitRepoRootFn,
) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in extract_repo_paths_from_prompt(prompt):
        repo_root = git_repo_root_fn(candidate)
        if repo_root is None:
            continue
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def resolve_linked_repo_roots(
    *,
    linked_repo_paths: list[str],
    prompt: str | None,
    git_repo_root_fn: GitRepoRootFn,
) -> list[Path]:
    repo_roots: list[Path] = []
    seen: set[Path] = set()
    explicit_roots = resolve_explicit_linked_repo_roots(linked_repo_paths, git_repo_root_fn=git_repo_root_fn)
    prompt_roots = resolve_prompt_linked_repo_roots(prompt, git_repo_root_fn=git_repo_root_fn)
    for repo_root in [*explicit_roots, *prompt_roots]:
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        repo_roots.append(repo_root)
    return repo_roots


def prepare_auto_commit_states(
    run_cwd: Path,
    prompt: str | None,
    run_logger: RunLogger,
    *,
    linked_repo_paths: list[str] | None = None,
    git_binary_available_fn: GitBinaryAvailableFn,
    git_repo_root_fn: GitRepoRootFn,
    git_status_has_changes_fn: GitStatusHasChangesFn,
) -> list[AutoCommitState]:
    states = [
        prepare_auto_commit_state(
            run_cwd,
            run_logger,
            git_binary_available_fn=git_binary_available_fn,
            git_repo_root_fn=git_repo_root_fn,
            git_status_has_changes_fn=git_status_has_changes_fn,
        )
    ]
    seen_roots = {states[0].repo_root.resolve(strict=False)}
    for repo_root in resolve_linked_repo_roots(
        linked_repo_paths=linked_repo_paths or [],
        prompt=prompt,
        git_repo_root_fn=git_repo_root_fn,
    ):
        resolved_root = repo_root.resolve(strict=False)
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)
        states.append(
            build_auto_commit_state(
                repo_root,
                run_logger,
                label=f"linked repo {repo_root}",
                git_binary_available_fn=git_binary_available_fn,
                git_repo_root_fn=git_repo_root_fn,
                git_status_has_changes_fn=git_status_has_changes_fn,
            )
        )
    return states


def managed_repo_roots(auto_commits: list[AutoCommitState]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for auto_commit in auto_commits:
        resolved_root = auto_commit.repo_root.resolve(strict=False)
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        roots.append(auto_commit.repo_root)
    return roots


def sandbox_writable_roots(auto_commits: list[AutoCommitState]) -> list[str]:
    return [str(root.resolve(strict=False)) for root in managed_repo_roots(auto_commits)]


def log_run_scope(
    run_logger: RunLogger,
    *,
    auto_commits: list[AutoCommitState],
    sandbox_mode: str,
) -> None:
    run_logger.write_line(f"sandboxMode={sandbox_mode}")
    for index, repo_root in enumerate(managed_repo_roots(auto_commits), start=1):
        run_logger.write_line(f"managedRepo{index}={repo_root.resolve(strict=False)}")
    if sandbox_mode == "workspace-write":
        for index, writable_root in enumerate(sandbox_writable_roots(auto_commits), start=1):
            run_logger.write_line(f"sandboxWritableRoot{index}={writable_root}")


def validate_sandbox_scope(*, auto_commits: list[AutoCommitState], sandbox_mode: str) -> None:
    if sandbox_mode == "workspace-write" and not sandbox_writable_roots(auto_commits):
        raise AppServerError("workspace-write sandbox requires at least one writable root")


def maybe_commit_checkpoint(
    auto_commit: AutoCommitState,
    run_logger: RunLogger,
    message: str,
    *,
    git_status_has_changes_fn: GitStatusHasChangesFn,
    git_add_all_fn: GitAddAllFn,
    git_commit_fn: GitCommitFn,
) -> None:
    if not auto_commit.enabled:
        return
    has_changes = git_status_has_changes_fn(auto_commit.repo_root, auto_commit.excluded_relative_paths)
    if has_changes is None:
        run_logger.write_line("[commit] skipping checkpoint: failed to inspect git status")
        return
    if not has_changes:
        run_logger.write_line(f"[commit] skipping `{message}`: no changes to commit")
        return
    add_result = git_add_all_fn(auto_commit.repo_root, auto_commit.excluded_relative_paths)
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip() or "git add failed"
        run_logger.write_line(f"[commit] failed `{message}`: {detail}", to_terminal=True, stream="stderr")
        return
    commit_result = git_commit_fn(auto_commit.repo_root, message)
    if commit_result.returncode != 0:
        detail = (commit_result.stderr or commit_result.stdout).strip() or "git commit failed"
        run_logger.write_line(f"[commit] failed `{message}`: {detail}", to_terminal=True, stream="stderr")
        return
    run_logger.write_line(f"[commit] created `{message}`")


def maybe_commit_checkpoints(
    auto_commits: list[AutoCommitState],
    run_logger: RunLogger,
    message: str,
    *,
    git_status_has_changes_fn: GitStatusHasChangesFn,
    git_add_all_fn: GitAddAllFn,
    git_commit_fn: GitCommitFn,
) -> None:
    for auto_commit in auto_commits:
        maybe_commit_checkpoint(
            auto_commit,
            run_logger,
            message,
            git_status_has_changes_fn=git_status_has_changes_fn,
            git_add_all_fn=git_add_all_fn,
            git_commit_fn=git_commit_fn,
        )


def maybe_push_checkpoint(
    auto_commit: AutoCommitState,
    run_logger: RunLogger,
    *,
    git_has_upstream_fn: GitHasUpstreamFn,
    git_push_fn: GitPushFn,
) -> None:
    if not auto_commit.enabled:
        return
    if not git_has_upstream_fn(auto_commit.repo_root):
        run_logger.write_line(f"[push] skipping {auto_commit.repo_root}: no upstream configured")
        return
    push_result = git_push_fn(auto_commit.repo_root)
    if push_result.returncode != 0:
        detail = (push_result.stderr or push_result.stdout).strip() or "git push failed"
        run_logger.write_line(f"[push] failed for {auto_commit.repo_root}: {detail}", to_terminal=True, stream="stderr")
        return
    run_logger.write_line(f"[push] pushed {auto_commit.repo_root}")


def maybe_push_checkpoints(
    auto_commits: list[AutoCommitState],
    run_logger: RunLogger,
    *,
    git_has_upstream_fn: GitHasUpstreamFn,
    git_push_fn: GitPushFn,
) -> None:
    for auto_commit in auto_commits:
        maybe_push_checkpoint(
            auto_commit,
            run_logger,
            git_has_upstream_fn=git_has_upstream_fn,
            git_push_fn=git_push_fn,
        )

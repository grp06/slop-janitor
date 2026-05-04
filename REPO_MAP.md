# Repo Map

`slop-janitor` is a CLI orchestrator for running Codex skills against a linked
repository. It owns the workflow shape, logging, retries, checkpoint timing, and
artifact verification; the linked repository owns the actual code changes and
`.agent/` planning artifacts.

## Top Level

- `README.md` documents install, CLI usage, `janitor` mode, `builder` mode, and
  the expected safety model.
- `CONTRIBUTING.md` documents local development setup and the unittest command.
- `ARCHITECTURE.md` explains the runtime design and ownership boundaries.
- `REPO_MAP.md` is this orientation map.
- `slop-janitor` is the executable launcher script.
- `scripts/sync-skills-from-home.sh` updates bundled skills from the user's
  global skill directory.
- `runs/` contains local run logs and state snapshots. Treat it as runtime
  output, not source.

## Python Package

- `slop_janitor/cli.py`
  - Parses CLI arguments, including `janitor`, `builder`, and the legacy
    no-subcommand shorthand.
  - Starts threads, executes stages, handles retries, applies delays, and
    checkpoints linked repositories.
  - Performs mode-level validation, including builder create-new, existing-plan,
    and review requirements.

- `slop_janitor/workflow_topology.py`
  - Builds the ordered stage lists for `janitor` and `builder`.
  - Owns stage labels, terminal phase names, checkpoint boundaries, thread-start
    boundaries, and clean-start/clean-end expectations.
  - Centralizes fixed workflow skills such as `execplan-improve` and
    `review-recent-work`.

- `slop_janitor/workflow_state.py`
  - Reads and validates workflow artifacts in linked repositories.
  - Captures stage snapshots, tracks `.agent/` artifact paths, identifies dirty
    paths allowed by each stage, and verifies builder meta-plan transitions.
  - Knows the filesystem contract for `.agent/work/` and `.agent/meta-plans/`.

- `slop_janitor/goal_state.py`
  - Reads, validates, and updates `.agent/goals/<id-slug>/` artifacts.
  - Selects the next ready goal, formats the Codex thread goal objective, and
    appends lifecycle events to `ledger.jsonl`.

- `slop_janitor/app_server.py`
  - Starts and talks to the Codex app-server subprocess over JSON-RPC.
  - Owns low-level request/response framing, process lifecycle, and direct
    `thread/goal/*` JSON-RPC helpers.

- `slop_janitor/turn_session.py`
  - Drives individual Codex turns through the app-server.
  - Reduces event streams into terminal output, token usage, assistant text, and
    tool/server request state.

- `slop_janitor/managed_repos.py`
  - Discovers managed git repositories under the Codex workspace.
  - Checks cleanliness, creates checkpoint commits, and pushes when configured.

- `slop_janitor/models.py`
  - Contains shared dataclasses used across the CLI, topology, logging, and
    app-server layers.

- `slop_janitor/run_log.py`
  - Names run log files and state files.
  - Splits terminal-facing output from durable run logs.

## Bundled Skills

Bundled skills live under `.agents/skills/`. These are the contracts that the
runtime injects into Codex stages.

- `find-refactor-candidates` finds janitor candidates.
- `select-refactor` chooses one janitor candidate.
- `execplan-create` creates a child ExecPlan from either a janitor decision or
  an active meta-plan slice.
- `execplan-improve` improves the current child ExecPlan.
- `implement-execplan` implements the current child ExecPlan.
- `review-recent-work` reviews implementation work and, for builder child work,
  reconciles the completed slice back to the parent meta-plan.
- `create-meta-plan` creates the parent builder plan under
  `.agent/meta-plans/`.
- `create-goals` creates a sequential goal plan under `.agent/goals/`.
- `complete-goals` launches the approved active goal plan through
  `slop-janitor goals run`.

Subagent skill files may exist in the tree for compatibility, but the public CLI
does not expose skill-selection flags.

## Agent Artifact Directories

These directories may exist either in this repository or in a linked target
repository, depending on the run.

- `.agent/PLANS.md` is the ExecPlan style guide used by planning skills.
- `.agent/work/<work-id>/` stores child work items, decisions, exec plans,
  status metadata, and review artifacts.
- `.agent/meta-plans/<meta-plan-id>/` stores builder parent plans.
- `.agent/goals/<goal-plan-id>/` stores goal-mode plans as `brief.md`,
  `goals.json`, and `ledger.jsonl`.
- `.agent/goals/active` points at the active goal plan.
- `.agent/meta-plans/active` points at the active parent meta-plan.
- `.agent/active` points at the active child work item.
- `.agent/done/` stores completed work items when skills archive them.

## Tests

- `tests/test_cli.py` covers parser compatibility, topology, recovery behavior,
  checkpoint timing, and mode-specific runtime behavior.
- `tests/fixtures/fake_app_server.py` simulates app-server responses and writes
  deterministic `.agent/` artifacts for integration-style CLI tests.

## Primary Workflows

Janitor mode runs the existing one-refactor loop:

1. `find-refactor-candidates`
2. `select-refactor`
3. `execplan-create`
4. `execplan-improve` repeated by `--improvements`
5. `implement-execplan`
6. `review-recent-work` repeated by `--review`

Builder mode runs a bounded multi-slice project loop:

1. `create-meta-plan`
2. For each requested slice, in a fresh Codex thread:
   - `execplan-create`
   - `execplan-improve` repeated by `--improvements`
   - `implement-execplan`
   - `review-recent-work` repeated by `--review`

When builder is launched with `--meta-plan PATH`, it skips `create-meta-plan`,
points `.agent/meta-plans/active` at that parent plan, derives the number of
remaining slice attempts from `slices.json`, and starts directly at
`slice-1-execplan-create`.

Goal mode runs an approved `.agent/goals/<id-slug>/` plan, or resolves
`.agent/goals/active` when no path is supplied:

1. Validate `brief.md`, `goals.json`, and `ledger.jsonl`.
2. Select the active or first ready goal.
3. Start a Codex thread and call `thread/goal/set`.
4. Start a text turn that pursues the active thread goal.
5. Require `thread/goal/get` to report `complete`.
6. Update `goals.json`, append `ledger.jsonl`, checkpoint, and advance.

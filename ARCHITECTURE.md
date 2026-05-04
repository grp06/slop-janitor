# Architecture

`slop-janitor` is a workflow runner around Codex app-server. It does not decide
what code to change directly. Instead, it starts Codex threads with bundled
skills, verifies the artifacts those skills produce, and checkpoints linked
repositories at deterministic boundaries.

The main design goal is a small CLI surface with the sequencing complexity kept
inside the runtime.

## Runtime Shape

The CLI has two user-facing workflow modes:

- `janitor` finds and implements one refactor per cycle. This is also the
  backwards-compatible default when no mode is supplied.
- `builder` creates a bounded meta-plan from a user prompt or executes an
  existing active meta-plan, then runs each slice as a normal child ExecPlan.
- `goals run` executes an approved sequential goal plan from `.agent/goals/`.
  When no path is supplied, it resolves `.agent/goals/active`. Goal artifacts
  are the source of truth; Codex thread goals are temporary execution context
  for one goal at a time.

Both modes share the same lower-level runtime:

1. Parse and validate arguments in `slop_janitor/cli.py`.
2. Locate the Codex workspace and optional linked repository.
3. Load bundled skills from `.agents/skills/`.
4. Build a stage topology in `slop_janitor/workflow_topology.py`.
5. Start the app-server through `slop_janitor/app_server.py`.
6. Drive each Codex turn through `slop_janitor/turn_session.py`.
7. Inspect filesystem artifacts through `slop_janitor/workflow_state.py`.
8. Checkpoint linked repositories through `slop_janitor/managed_repos.py`.
9. Write terminal output and durable run logs through `slop_janitor/run_log.py`.

Goal mode uses the same app-server, logging, sandbox, and checkpoint layers, but
loads its plan through `slop_janitor/goal_state.py` instead of the work-item or
meta-plan helpers.

## Component Boundaries

### CLI And Runner

`slop_janitor/cli.py` owns the public interface and the orchestration loop. It
decides which mode is running, validates counts and required flags, starts new
threads at topology-defined boundaries, retries recoverable failures, applies
cycle/slice delays, and asks other modules for stage labels, checks, and git
operations.

It should not contain detailed knowledge of `.agent/` layouts beyond calling the
workflow-state helpers that own those contracts.

### Workflow Topology

`slop_janitor/workflow_topology.py` owns the shape of each workflow. This keeps
stage construction, checkpoint timing, terminal labels, and thread-start rules in
one place.

Janitor stages preserve the original loop. Builder stages are generated from
either the requested slice count or the remaining slices in an existing
meta-plan, and use slice-qualified labels such as `slice-1-execplan-create`.

### Workflow State

`slop_janitor/workflow_state.py` owns filesystem interpretation for workflow
artifacts. It knows how to find active work items, active meta-plans, primary
stage artifacts, expected dirty paths, and builder reconciliation state.

Builder uses this module for deterministic checks:

- after `create-meta-plan`, an active parent meta-plan, `meta.json`, and
  `slices.json` must exist;
- the parent must contain exactly the requested number of slices;
- with `--meta-plan`, the provided parent plan must be active, readable, and
  have a valid active slice before the app-server starts;
- before each slice, the parent must still be active unless already completed;
- after final review, the parent must advance, complete, or explicitly block.

### Goal State

`slop_janitor/goal_state.py` owns `.agent/goals/<id-slug>/` and
`.agent/goals/active`. It validates `brief.md`, `goals.json`, and
`ledger.jsonl`, selects the next ready goal, formats the goal objective sent to
Codex, and persists goal lifecycle events.

The runner keeps the policy simple: ordered goals only, with optional
`depends_on` for real blockers. There is no DAG engine or pipeline scheduler.

### App-Server Client

`slop_janitor/app_server.py` owns the JSON-RPC process boundary. It starts the
app-server, sends requests, reads responses, and terminates the process. Higher
layers should treat it as a transport, not a workflow engine.

It also exposes the direct Codex goal methods `thread/goal/set`,
`thread/goal/get`, and `thread/goal/clear`. Callers should not emulate goals by
sending slash commands.

### Turn Session

`slop_janitor/turn_session.py` owns one Codex turn. It consumes app-server
events, renders assistant/tool progress, records token usage, handles server
request notifications, and returns a structured result to the runner.

### Managed Repositories

`slop_janitor/managed_repos.py` owns git repository discovery and checkpointing.
The runner asks it whether linked repos are clean, what changed, and whether a
checkpoint commit or push should happen.

### Skills

`.agents/skills/` contains the prompt contracts used by stages. The runtime
injects these skills and verifies the artifacts they create, but the skills own
the natural-language instructions for planning, implementation, and review.

The public CLI intentionally uses fixed improvement and review skills:
`execplan-improve` and `review-recent-work`.

## Janitor Flow

Janitor mode is optimized for one bounded refactor.

1. Candidate discovery writes a shortlist.
2. Selection locks one candidate into an active work item.
3. ExecPlan creation turns the selected work item into an implementation plan.
4. Improvement passes rewrite that plan in place.
5. Implementation executes the plan.
6. Review passes inspect recent work, fix obvious issues, and record review
   results.

The loop can repeat with `--cycles`. Delays apply between cycles.

## Builder Flow

Builder mode is optimized for a larger project that still needs bounded child
work.

For a new project, `create-meta-plan` turns the required user prompt into
exactly the requested number of slices under `.agent/meta-plans/<id>/`.

For an existing project, `builder --meta-plan PATH` validates the parent plan,
points `.agent/meta-plans/active` at it, counts remaining slices from the active
slice onward, and starts directly with child ExecPlan creation.

In both cases, the runtime executes each slice as a fresh child workflow in a
fresh Codex thread.

For each slice:

1. `execplan-create` consumes the active parent slice and creates a child work
   item under `.agent/work/<id>/`.
2. `execplan-improve` improves the child ExecPlan.
3. `implement-execplan` implements the child plan.
4. `review-recent-work` reviews the child work and reconciles the slice back to
   the parent meta-plan.

Builder requires either `--prompt` plus `--slices`, or `--meta-plan PATH`.
It always requires at least one review pass because the final review owns parent
advancement.

## Goal Flow

Goal mode is optimized for a lighter planning surface than builder mode.

1. The user chats with Codex and invokes `create-goals`.
2. `create-goals` writes `.agent/goals/<id-slug>/brief.md`,
   `.agent/goals/<id-slug>/goals.json`, and
   `.agent/goals/<id-slug>/ledger.jsonl`.
3. `create-goals` points `.agent/goals/active` at the new plan.
4. The user reviews or edits the artifact.
5. `complete-goals`, or `slop-janitor goals run`, validates the active plan.
6. The runner selects the next ready goal, starts a Codex thread, calls
   `thread/goal/set`, and starts a normal text turn telling Codex to pursue the
   active thread goal.
7. The runner requires `thread/goal/get` to report `complete` before it marks
   the artifact goal completed.
8. The result and evidence are written back to `goals.json` and `ledger.jsonl`,
   then the managed repositories are checkpointed before the next goal.

## Checkpointing

Checkpoint boundaries are defined by topology, not by individual skills.

Janitor checkpoints after final planning, implementation, and final review for
each cycle. Builder checkpoints after meta-plan creation when present, final
planning for each slice, each slice implementation, and each final slice review.

This keeps git commits aligned with durable workflow milestones rather than raw
turn boundaries.

## Failure Model

The runner fails early for invalid CLI arguments, missing bundled skills,
unclean managed repositories at clean-start boundaries, missing required
artifacts, or builder state transitions that do not reconcile.

Recoverable app-server failures can be retried according to the retry flags.
Workspace changes are captured before a stage and used to prevent unsafe replay
when recovery would otherwise hide a changed filesystem state.

## Extension Points

To add a new workflow mode, extend the parser in `cli.py`, add stage construction
and topology helpers in `workflow_topology.py`, add any required artifact checks
in `workflow_state.py`, and cover the mode in `tests/test_cli.py` with the fake
app-server fixture.

To change a skill contract, update the bundled skill under `.agents/skills/`,
then update README examples and tests that assert stage text or artifact shape.

To change checkpoint behavior, update topology first. The runner should continue
to ask topology whether a stage is a checkpoint boundary.

## Testing Strategy

Most behavior is tested through `tests/test_cli.py` with
`tests/fixtures/fake_app_server.py`. The fake server simulates app-server
responses and writes deterministic `.agent/` artifacts, which lets tests cover
runtime sequencing without calling real Codex.

Use the repository test command:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

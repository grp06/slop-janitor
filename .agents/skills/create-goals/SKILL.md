---
name: create-goals
description: >-
  Turn a freeform idea, brief, or discussion into a durable sequential goal
  plan under .agent/goals/ for later execution by slop-janitor goals run.
---

# Create Goals

Create a minimal, ordered goal plan from the current conversation. This skill is
interactive planning only. Do not implement code, run tests, or start executing
the goals.

## Goal

Turn the user's idea into a durable plan under `.agent/goals/<id-slug>/`:

- `brief.md`
- `goals.json`
- `ledger.jsonl`

Also point `.agent/goals/active` at the plan directory so the user can run or
invoke the approved plan without retyping its path.

The artifact is the source of truth. Codex thread goals are only the execution
context used later by `slop-janitor goals run`.

## Workflow

1. Distill the user's objective, constraints, non-goals, assumptions, risks,
   stop conditions, and validation expectations.
2. Ask concise clarifying questions only when the next goal sequence would be
   materially wrong without the answer. If a question can be answered by reading
   the repo, read the repo instead.
3. Create `.agent/goals/<id-slug>/`.
4. Write `brief.md` as stable human-readable context.
5. Write `goals.json` with an ordered `goals` array.
6. Initialize `ledger.jsonl` with a `plan_created` event.
7. Create or update `.agent/goals/active` as a symlink to `<id-slug>`.
8. Stop and ask the user to approve or edit the plan before execution.

## Artifact Model

`goals.json` must be a JSON object:

```json
{
  "id": "2026-05-03-my-goal-plan",
  "title": "My goal plan",
  "status": "active",
  "active_goal_id": "goal-1",
  "created_at": "2026-05-03T12:00:00Z",
  "updated_at": "2026-05-03T12:00:00Z",
  "goals": [
    {
      "id": "goal-1",
      "title": "First goal",
      "objective": "Concrete objective for Codex to pursue.",
      "rationale": "Why this goal comes first.",
      "scope": ["Included work"],
      "non_goals": ["Explicitly excluded work"],
      "stop_condition": "Concrete condition that ends this goal.",
      "acceptance_criteria": ["Observable success criterion"],
      "validation": ["Exact command, artifact, or inspection expected"],
      "depends_on": [],
      "status": "ready",
      "result_summary": null,
      "evidence": [],
      "risks": ["Known risk"],
      "assumptions": ["Planning assumption"]
    }
  ]
}
```

Allowed plan statuses: `active`, `blocked`, `completed`, `abandoned`.

Allowed goal statuses: `ready`, `active`, `blocked`, `completed`, `failed`,
`skipped`.

Set `active_goal_id` to the first executable goal. Keep dependencies simple:
prefer ordered goals and use `depends_on` only for real blockers.

Use a relative symlink for the active pointer:

```bash
ln -sfn <id-slug> .agent/goals/active
```

If the platform cannot create symlinks, stop and tell the user to pass the
explicit plan path to `slop-janitor goals run .agent/goals/<id-slug>`.

## Brief Shape

Keep `brief.md` compact:

- title
- objective
- constraints
- non-goals
- ordered goal summary
- acceptance and validation expectations
- assumptions and risks

## Anti-Patterns

- executing the plan while creating it
- invoking `complete-goals` before the user approves the artifacts
- hiding important criteria only in prose
- creating a DAG or pipeline engine in JSON
- making every goal depend on every previous goal without a real dependency
- dumping transcript history into artifacts

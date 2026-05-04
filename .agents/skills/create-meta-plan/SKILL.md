---
name: create-meta-plan
description: >-
  Create a long-horizon meta-plan from a user brief, PRD, RFC, or dictated
  discussion. Use when the user wants to capture a multi-slice project in a
  durable parent artifact before creating child ExecPlans.
---

# Create Meta-Plan

Create a thin parent planning object that holds the durable long-horizon brief,
ordered execution slices, and current frontier for future child ExecPlans.

This skill is planning-only. It must not create child work items or child
ExecPlans.

## Goal

Turn a long freeform discussion into a durable parent artifact under
`.agent/meta-plans/` that:

- captures the stable objective and constraints
- breaks the work into the requested number of bounded slices
- marks the next slice to execute
- stays small enough to update over many child ExecPlan cycles

The meta-plan is a parent object. It does not replace `.agent/work/<id>/`
child work items.

## Artifact Model

Use this directory shape:

- `.agent/meta-plans/<id-slug>/meta.json`
- `.agent/meta-plans/<id-slug>/brief.md`
- `.agent/meta-plans/<id-slug>/slices.json`
- `.agent/meta-plans/active` as a convenience symlink

### `meta.json`

Keep it intentionally small:

```json
{
  "id": "2026-04-22-my-meta-plan",
  "slug": "my-meta-plan",
  "title": "My meta plan",
  "created_at": "2026-04-22T18:00:00Z",
  "updated_at": "2026-04-22T18:00:00Z",
  "status": "active",
  "active_slice_id": "slice-1",
  "artifacts": {
    "brief": "brief.md",
    "slices": "slices.json"
  }
}
```

Allowed `status` values:

- `active`
- `blocked`
- `completed`
- `abandoned`

### `brief.md`

Keep it human-readable and stable. Include:

- distilled objective
- constraints
- non-goals
- success criteria
- important assumptions from the user discussion

### `slices.json`

This is the machine-readable frontier. Store a JSON array. Each slice should
contain:

- `id`
- `title`
- `summary`
- `status`
- optional `depends_on`
- `child_work_item_id`
- `child_work_item_path`
- `result_summary`
- `notes`

Allowed slice `status` values:

- `ready`
- `active`
- `blocked`
- `completed`

`notes` must stay short and structured. Do not dump transcript history into
them.

## Input Resolution

Preferred source input:

1. explicit user brief, PRD, RFC, dictated notes, or detailed discussion in
   the current thread
2. explicit existing meta-plan path supplied by the user when they want to
   refresh or replace it
3. `.agent/meta-plans/active` only when the user clearly asks to revise the
   active meta-plan in place

Do not silently create a new meta-plan when the user clearly intends to revise
an existing one.

## Workflow

### Step 1: Read the planning source

Extract:

- the durable objective
- must-have constraints
- out-of-scope areas
- success criteria
- the obvious major slices

Prefer first-principles compression over paraphrasing the whole discussion.

### Step 2: Resolve or create the meta-plan directory

If updating an existing meta-plan:

- read `meta.json`
- read `brief.md`
- read `slices.json`

If creating a new one:

- create `.agent/meta-plans/` if missing
- derive a descriptive id/slug from the project theme
- create `.agent/meta-plans/<id-slug>/`
- initialize `meta.json`
- write `.agent/meta-plans/active` to point at it

### Step 3: Distill the brief

Write `brief.md` with:

- title
- objective
- constraints
- non-goals
- success criteria
- assumptions

Keep the brief stable. It should explain the project at the parent level, not
the details of any single child ExecPlan.

### Step 4: Create bounded slices

Produce the exact number of slices requested by the user or caller.

Rules:

- prefer one slice per likely child ExecPlan
- preserve the user's intended order
- keep slices bounded and independently understandable
- use `depends_on` only for real blocking relationships
- otherwise rely on order alone

Each slice must be specific enough that `execplan-create` can later derive a
child `decision.md` from it without reopening the whole project search space.

### Step 5: Mark the frontier

- mark exactly one slice as `active`, or if you cannot justify an active
  selection safely, mark the first executable slice as `ready` and set
  `active_slice_id` to it
- keep later slices `ready` only when they are logically available
- keep blocked slices `blocked`

For V1, prefer exactly one `active` slice and the rest `ready` or `blocked`.

### Step 6: Finalize metadata

Write `meta.json` with:

- `status="active"` unless the user explicitly wants a blocked or archived
  parent plan
- `active_slice_id=<next slice id>`
- `updated_at=<now>`
- `artifacts.brief="brief.md"`
- `artifacts.slices="slices.json"`

## Anti-Patterns

- creating child work items or child ExecPlans
- writing giant prose blobs into `notes`
- creating slices so broad they require major rescoping before planning
- making every later slice depend on every earlier slice without evidence
- duplicating child implementation detail into the parent brief

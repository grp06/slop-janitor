---
name: execplan-improve-subagents
description: >-
  Improves an existing ExecPlan with a two-wave subagent workflow: first
  specialized reviewers audit factual accuracy, adjacent code, design quality,
  validation, and self-containment, then closure reviewers hunt residual gaps
  after a provisional rewrite before the parent produces the final plan. Use
  when the user asks to improve an execplan with subagents, audit a plan using
  parallel reviewers, strengthen an execplan via delegation, or says
  "execplan-improve-subagents".
---

# Improve ExecPlan With Subagents

> **Core philosophy:** subagents gather distinct, code-grounded evidence in parallel; the parent agent alone rewrites the ExecPlan. No speculative additions. No surface-level rewording. If there is nothing material to improve, return exactly `skip`.

## Subagent Strategy

Use subagents aggressively for this skill. The default shape is a two-wave review:

1. Wave 1 spawns eight dedicated specialist reviewers on the original plan.
2. The parent rewrites a provisional improved draft in place.
3. Wave 2 spawns two closure reviewers on the rewritten draft.
4. The parent applies any final closure fixes and returns the final summary.

Wave 1 child agents:

- `execplan_reality_checker`
- `execplan_adjacency_mapper`
- `execplan_interface_depth_critic`
- `execplan_complexity_pulldown_critic`
- `execplan_concept_count_critic`
- `execplan_boundary_ownership_critic`
- `execplan_validation_observability_critic`
- `execplan_novice_reader_critic`

Wave 2 child agents:

- `execplan_residual_gap_hunter`
- `execplan_closure_critic`

The parent agent is the only writer. Subagents do not edit files and do not rewrite the ExecPlan independently.

## Ousterhout Lens

Use John Ousterhout's design philosophy as the design-quality lens for the audit:

- prefer deep modules over shallow wrappers
- prefer interfaces that hide sequencing and policy details
- prefer fewer concepts, fewer knobs, and fewer special cases
- prefer simpler mental models over visually tidy decomposition
- prefer moving complexity behind a stable boundary over redistributing it

Treat these as the main forms of complexity:

- change amplification
- cognitive load
- unknown unknowns

An improved plan is not just more accurate. It should also be clearer about why the target design is simpler and what complexity the change removes from the rest of the system.

## Resolving the Base Repo

You may be running from a Codex worktree such as `~/.codex/worktrees/<id>/<repo>/`.

1. Check if the current working directory contains `/.codex/worktrees/` in its path.
2. If yes, extract the repo name from the last path component and set the base repo to `~/<repo-name>`.
3. If no, the base repo is the current working directory.

When looking for `.agent/` contents such as work items, legacy ExecPlans, and `PLANS.md`, check both the worktree `.agent/` and the base repo `.agent/`. Prefer the worktree copy if both exist.

## Inputs

Preferred target resolution order:

1. explicit plan path supplied by the user
2. explicit work-item path supplied by the user
3. `.agent/active` when it points to a work item with `stage="plan"` and `state="completed"`
4. the most recently updated work item under `.agent/work/` with `stage="plan"` and `state="completed"`
5. legacy fallback: `.agent/execplan-pending.md`
6. explicit legacy fallback: `.agent/potential-bugs/<plan-name>.md`

If no ExecPlan exists in any supported location, tell the user and stop.

## Workflow

### Step 0: Short-Circuit Low-Value Repeats

Before doing repo work, inspect only the immediately previous assistant turn in the current conversation.

- If the previous result was exactly `skip`, return exactly `skip`.
- If it ended with `Usefulness score: N/10 - ...` and `N <= 3`, return exactly `skip`.
- In either skip case, do not read `PLANS.md`, do not spawn subagents, and do not rewrite the plan.

### Step 1: Read the plan contract and locate the ExecPlan

Read `.agent/PLANS.md` from the base repo or worktree before modifying the ExecPlan.

If operating on a work item, read:

- `meta.json`
- `decision.md` when present
- `execplan.md`

Otherwise read the resolved legacy plan path directly.

### Step 2: Spawn wave 1

Spawn all eight wave 1 reviewer agents. Give each child:

- the ExecPlan path
- the current working directory
- the resolved base repo path
- a reminder that it is read-only and must not edit files
- a request to return concrete findings plus exact plan-update recommendations
- a request to name exact file paths, symbols, tests, routes, mocks, commands, and ownership boundaries when relevant
- a request to classify caller status as production-used, test-only, or unused when that distinction matters
- a reminder to return exactly `skip` if it finds no material, code-grounded improvement in its lane

Wait for all child agents to finish before changing the ExecPlan.

### Step 3: Synthesize wave 1 evidence

Merge the wave 1 findings into one consolidated edit plan.

- Deduplicate overlapping findings.
- Prefer factual corrections over speculative improvements.
- Prefer changes backed by multiple reviewers when they agree.
- Reject any child recommendation that is not grounded in code or that changes the plan's intent.
- Ignore child results that are exactly `skip`.
- If every wave 1 child returns exactly `skip`, return exactly `skip` and do not rewrite the ExecPlan.

### Step 4: Write the provisional rewrite

Rewrite the ExecPlan in place using the wave 1 evidence.

Preserve existing `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` content.

Apply only code-grounded improvements:

- fix inaccuracies such as wrong paths, signatures, commands, and assumptions
- add missing files, tests, dependencies, milestones, and verification steps
- split oversized milestones when needed
- define undefined jargon
- make acceptance criteria observable and verifiable
- add idempotence and recovery guidance where missing
- make the plan explicit about the simpler boundary it is trying to create
- name the complexity dividend: what future readers or callers no longer need to know after the change

Do not change the plan's intent. Do not add milestones that do not serve the original purpose.

### Step 5: Spawn wave 2 closure reviewers

After the provisional rewrite is saved, spawn both closure reviewers:

- `execplan_residual_gap_hunter`
- `execplan_closure_critic`

Give each child:

- the rewritten ExecPlan path
- the current working directory
- the resolved base repo path
- a reminder that this is a second-wave closure pass over an already-improved draft
- a reminder that it is read-only and must not edit files
- a request to focus on residual cross-lane omissions, contradictions, and end-to-end completeness
- a reminder to return exactly `skip` if it finds no remaining material issue

Wait for both closure reviewers to finish.

### Step 6: Apply closure fixes and finalize the ExecPlan

Rewrite in place at the same file path.

Use the closure reviewers only to catch residual gaps. Ignore closure results that are exactly `skip`.

If the full subagent review finds no substantive code-grounded improvements beyond the existing draft, do not churn the prose just to make a diff.

### Step 7: Score the usefulness of the pass

Score the usefulness of this invocation, not the absolute quality of the final plan.

- `9-10/10`: the pass fixed multiple concrete execution blockers or major missing dependencies, and the implementation path would likely have failed without these changes.
- `7-8/10`: the pass added several substantive, code-grounded corrections that materially improve executability.
- `4-6/10`: the pass made real but moderate improvements; the plan is clearer and safer, but not fundamentally different.
- `1-3/10`: the pass found little to improve beyond minor wording, sequencing, or already-obvious clarifications.

### Step 8: Summarize changes

Report to the user:

- **Fixed**: inaccuracies corrected
- **Added**: missing coverage added
- **Strengthened**: vague sections made concrete
- **Flagged**: risks or concerns worth attention
- Final line: `Usefulness score: X/10 - <specific reason>`

If Step 0 short-circuits, return exactly `skip` and nothing else.

If every wave 1 child returns exactly `skip`, return exactly `skip` and nothing else.

## Anti-Patterns

- **Parallel rewriting:** subagents must not edit the ExecPlan directly.
- **Surface-level rewording:** changing prose without code evidence is worthless.
- **Speculative additions:** every addition must trace back to the repository.
- **Duplicated synthesis:** the parent must merge and deduplicate; do not paste child reviews into the final output.
- **Changing intent:** improve execution detail and design clarity without second-guessing the underlying goal.

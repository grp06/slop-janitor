---
name: review-recent-work-subagents
description: >-
  Run a two-wave subagent review-and-fix pass on the most recently implemented
  ExecPlan or a user-specified completed ExecPlan. Specialized reviewer agents
  inspect contract correctness, regression surface, failure modes, state
  lifecycle, ownership, validation, and simplicity boundaries, then closure
  agents audit the synthesized fix plan. The parent agent implements the
  selected fixes, reruns verification, and reports the result. Use when you
  want post-implementation review after an ExecPlan lands, want independent
  review lanes that report back to the main agent, or want an
  Ousterhout-focused fresh-eyes audit that ends in concrete fixes.
---

# Review Recent Work With Subagents

> **Core philosophy:** subagents gather distinct, code-grounded review evidence in parallel; the parent agent alone synthesizes the final review, selects the bounded fixes, and edits code. No speculative findings. Child agents never edit files. If there is nothing material to fix or report, return exactly `skip`.

## Subagent Strategy

Use subagents aggressively for this skill. The default shape is a two-wave review:

1. Wave 1 spawns six dedicated specialist reviewers on the implemented change.
2. The parent agent synthesizes a provisional review and bounded fix plan.
3. Wave 2 spawns two closure reviewers on that provisional synthesis.
4. The parent agent implements the selected fixes, reruns verification, and returns the final review-and-fix report.

Preferred wave 1 child agents:

- `review_contract_correctness_checker`
- `review_regression_surface_mapper`
- `review_failure_mode_critic`
- `review_state_lifecycle_critic`
- `review_validation_observability_critic`
- `review_simplicity_boundary_critic`

Optional add-on specialist when the completed plan explicitly moved ownership boundaries, extracted a new seam, or consolidated lifecycle rules:

- `review_boundary_ownership_critic`

Preferred wave 2 child agents:

- `review_residual_gap_hunter`
- `review_closure_critic`

If the current session cannot spawn one of these custom agent types yet, fall back to a read-only `explorer` agent and paste the same lane responsibilities into that child prompt. The parent agent remains the only synthesizer and the only editor.

Subagents never edit files. The parent agent is responsible for every code change, validation command, and final prioritization decision.

## Ousterhout Lens

Use John Ousterhout's design philosophy as part of the review standard, not just correctness:

- prefer deep modules over shallow wrappers
- prefer interfaces that hide sequencing and policy details
- prefer fewer concepts, fewer knobs, and fewer special cases
- prefer simpler mental models over visually tidy decomposition
- prefer moving complexity behind a stable boundary over redistributing it

Treat these as the main forms of complexity:

- change amplification
- cognitive load
- unknown unknowns

The review-and-fix pass should answer two questions:

- is the new behavior correct
- is the implementation robust and still aligned with the plan's intended simplicity boundary

## Resolving the Base Repo

You may be running from a Codex worktree such as `~/.codex/worktrees/<id>/<repo>/`. Worktrees are shallow copies, so always resolve the base repo path:

1. Check whether the current working directory contains `/.codex/worktrees/`.
2. If yes, extract the repo name from the final path component and set the base repo to `~/<repo-name>`.
3. If no, the base repo is the current working directory.

When looking for `.agent/` contents, check both the worktree `.agent/` and the base repo `.agent/`. Prefer the worktree copy if both exist.

## Inputs

- Preferred: an explicit path to the completed ExecPlan you want reviewed.
- Default: the most recently modified Markdown file under `.agent/done/`.

If no completed ExecPlan exists, stop and tell the user.

## Workflow

### Step 0: Short-Circuit Low-Value Repeats

Before doing any repo work, inspect only the immediately previous assistant turn in the current conversation.

- If that immediately previous assistant turn was exactly `skip`, return exactly `skip`.
- If that immediately previous assistant turn clearly ended with `Usefulness score: N/10 - ...` and `N <= 3`, return exactly `skip`.
- In either skip case, do not inspect git state, do not read the ExecPlan, and do not spawn subagents.
- If the immediately previous assistant turn was not clearly a result from this skill, continue normally.

### Step 1: Resolve the review target and review surface

If the user supplied a completed ExecPlan path, use it.

Otherwise:

1. Search `.agent/done/` in both the worktree and base repo.
2. Select the most recently modified `*.md` file.
3. Treat that file as the implementation contract for this review pass.

Read the entire target ExecPlan. Extract the planned behavior, touched files, validation commands, acceptance criteria, and any risks or discoveries already recorded in the plan.

Inspect the repository before spawning subagents:

- `git status --short`
- `git diff --stat`
- `git diff`
- `git log -1 --stat --name-only`

Treat the review surface as the union of:

- files explicitly named in the completed ExecPlan
- files currently changed in git
- files touched by the most recent commit when the working tree is clean
- adjacent tests, helpers, fixtures, mocks, configs, and importers needed to judge correctness

If the latest implemented ExecPlan and the observable recent code changes clearly do not overlap, stop and report that mismatch rather than guessing.

### Step 2: Spawn wave 1

Spawn all six wave 1 reviewer agents. Give each child:

- the completed ExecPlan path
- the current working directory
- the resolved base repo path
- a concise summary of the review surface
- a reminder that it is read-only and must not edit files
- a request to return concrete findings plus exact review-update recommendations
- a request to propose the smallest safe fix direction, not a broad redesign
- a request to name exact file paths, symbols, tests, routes, fixtures, mocks, commands, and ownership boundaries when relevant
- a reminder to return exactly `skip` if it finds no material, code-grounded issue in its lane

Wait for all child agents to finish before synthesizing the review.

### Step 3: Synthesize wave 1 evidence

Merge the wave 1 findings into one provisional review and fix plan.

- Deduplicate overlapping findings.
- Reject speculative concerns or style-only nits.
- Prefer correctness and regression findings over polish.
- Prefer robustness findings over architectural commentary when both point at the same issue.
- Prefer issues backed by multiple reviewers when they agree.
- Ignore child results that are exactly `skip`.
- If every wave 1 child returns exactly `skip`, return exactly `skip` and nothing else.

Select a bounded fix set:

- fix issues that are high-confidence, code-grounded, and local to the reviewed implementation
- fix missing tests, missing assertions, broken branches, lifecycle bugs, cleanup gaps, and leaky boundaries when the repair is clear
- do not turn the review into a broad redesign or architecture rewrite
- leave open questions only for issues that are real but too ambiguous or too large to fix safely in this pass

Structure the provisional synthesis with:

- ordered findings by severity
- code evidence for each finding
- a minimal fix direction for each finding
- a short note on the robustness or complexity cost when that cost is non-obvious
- a clear distinction between `fix now` items and `leave as remaining risk` items
- open questions only when the code evidence is insufficient to fully resolve the issue

### Step 4: Spawn wave 2 closure reviewers

After the provisional synthesis is prepared, spawn both closure reviewers:

- `review_residual_gap_hunter`
- `review_closure_critic`

Give each child:

- the completed ExecPlan path
- the current working directory
- the resolved base repo path
- the provisional synthesized review and selected fix set inline in the prompt
- a reminder that this is a second-wave closure pass over an already-synthesized fix plan
- a reminder that it is read-only and must not edit files
- a request to focus on residual omissions, weak evidence, contradictions, bad prioritization, and obviously-missed high-confidence fixes
- a reminder to return exactly `skip` if it finds no remaining material issue

Wait for both closure reviewers to finish.

### Step 5: Finalize the fix set

Use the closure reviewers only to catch residual gaps. Ignore closure results that are exactly `skip`.

Before editing code:

- remove or downgrade any finding whose evidence is weak after closure review
- add any high-confidence omission the closure reviewers surfaced
- keep the fix set tight and local to the completed ExecPlan's implementation

### Step 6: Implement the selected fixes

Implement the `fix now` items. Do not stop at a findings-only report when the right fix is clear and bounded.

Prefer fixes that reduce complexity at the same time as they fix correctness, such as:

- moving repeated sequencing behind one owner
- tightening branch coverage around the newly introduced seam
- removing a leaky helper split introduced by the implementation
- adding targeted assertions for hidden-thread, cleanup, retry, or lifecycle paths

If a real issue is too ambiguous or too broad to fix safely in this pass, leave it as a remaining risk and explain why.

### Step 7: Re-run verification

Run the verification commands from the completed ExecPlan whenever they still apply. Add any targeted test or lint commands needed to validate the review fixes.

If verification cannot be run, say exactly why.

### Step 8: Summarize the pass

Return a review-and-fix report with findings first, followed by what changed and what was validated.

Report:

- findings ordered by severity, each with exact file evidence and a minimal fix direction
- what you changed
- what you validated
- open questions or assumptions, only when needed
- remaining testing or verification gaps
- whether the implementation stayed aligned with the plan's intended simplicity boundary or where it drifted
- final line: `Usefulness score: X/10 - <specific reason>`

If wave 2 finds no remaining issues, implement the original selected fix set and report the final result.

## Usefulness Scoring

Score the usefulness of the review-and-fix pass itself, not the absolute quality of the codebase.

- `9-10/10`: the pass caught and fixed a meaningful correctness bug, regression, or robustness failure that would likely have caused real trouble.
- `7-8/10`: the pass fixed several substantive issues and materially hardened the implementation.
- `4-6/10`: the pass made real but moderate improvements, such as tightening tests, validation, or edge-case handling.
- `1-3/10`: the review found little to improve beyond tiny polish, or confirmed the recent work was already in good shape.

The explanation must be concrete. Name the issue you fixed, or explain why the pass was low-value.

## Output Contract

When the pass runs, findings come first. Keep summaries brief and evidence-based.

If no material findings are needed after a real pass, say so plainly and still end with the usefulness score. Never return a findings-only report when there are safe, obvious fixes the parent could have made.

If Step 0 short-circuits, return exactly `skip` and nothing else.

If every wave 1 child returns exactly `skip`, return exactly `skip` and nothing else.

## Anti-Patterns

- **Review-only drift:** do not stop at findings when the right repair is clear and local.
- **Parallel fixing:** subagents must not edit code; the parent agent must make all edits.
- **Speculative findings:** every issue must trace back to the repository.
- **Finding inflation:** do not pad the report with style nits or weak concerns.
- **Duplicated synthesis:** the parent must merge and prioritize; do not paste eight raw reviews into the final output.
- **Review drift:** do not review unrelated older changes just because they are nearby.

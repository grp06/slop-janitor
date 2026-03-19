---
name: review-recent-work
description: >-
  Review the code changes from the most recently implemented ExecPlan with fresh eyes,
  fix obvious bugs and rough edges immediately, rerun verification, and report how useful
  the review pass was on a 1-10 scale. Use when you want a post-implementation review of
  the latest ExecPlan work instead of a generic "continue" response.
---

# Review Recent Work

Review the latest implemented ExecPlan as a fresh-eyes code review pass. Fix issues now when the right improvement is clear and bounded. End with a usefulness score for the review-and-fix pass itself.

This skill is intended to run immediately after `$implement-execplan`.

## Ousterhout lens

Use John Ousterhout's design philosophy as part of the review standard, not just correctness:

- prefer deep modules over shallow wrappers
- prefer interfaces that hide sequencing and policy details
- prefer fewer concepts and fewer special cases
- prefer simpler mental models over structurally elaborate designs
- prefer moving complexity behind a stable boundary over redistributing it

Treat these as the main forms of complexity:

- change amplification
- cognitive load
- unknown unknowns

The review should answer two questions:

- is the new behavior correct
- did the implementation actually make the system easier to understand and change

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

- If that immediately previous assistant turn was a `review-recent-work` result whose entire content was exactly `skip`, return exactly `skip`.
- If that immediately previous assistant turn was a `review-recent-work` result that ended with `Usefulness score: N/10 - ...` and `N <= 3`, return exactly `skip`.
- In either skip case, do not inspect git state, do not read the ExecPlan, and do not make code changes.
- If the immediately previous assistant turn was not clearly a `review-recent-work` result, continue normally.
- Do not scan further back in the conversation for older `review-recent-work` results.
- Only continue into the review workflow when there is no immediately previous `review-recent-work` result, or that immediate prior usefulness score is `4/10` or higher.

## Resolve the Review Target

If the user supplied a plan path, use it.

Otherwise:

1. Search `.agent/done/` in both the worktree and base repo.
2. Select the most recently modified `*.md` file.
3. Treat that file as the implementation contract for this review pass.

Read the entire target ExecPlan. Extract the planned behavior, touched files, validation commands, acceptance criteria, and any risks or discoveries already recorded in the plan.

## Build the Review Surface

Inspect the repository before making changes:

- `git status --short`
- `git diff --stat`
- `git diff`
- `git log -1 --stat --name-only`

Treat the review surface as the union of:

- files explicitly named in the completed ExecPlan
- files currently changed in git
- files touched by the most recent commit when the working tree is clean
- adjacent tests, helpers, and importers needed to judge correctness

If the latest implemented ExecPlan and the observable recent code changes clearly do not overlap, stop and report that mismatch rather than guessing.

### Step 1: Reconstruct intent

Use the completed ExecPlan to understand what the recent implementation was trying to achieve, how it is supposed to behave, and how it is meant to be verified.

### Step 2: Perform a real code review

Review the implementation with a fresh-eyes code review mindset. Prioritize:

- correctness bugs
- behavioral regressions
- missing error handling
- validation gaps
- missing or weak tests
- partial refactors and dead code
- mismatch with existing project patterns
- shallow wrappers or pass-through abstractions that hide little
- leaked sequencing or policy that should have been absorbed behind a boundary
- new concepts, branches, or configuration that increase interface burden without enough payoff
- missed opportunities to remove special cases the ExecPlan was supposed to simplify

### Step 3: Fix obvious issues now

If you find a clear improvement that is well-supported by the code and fits the review scope, make the fix immediately instead of only describing it.

Keep the scope tight. Do not spin the review into a broad refactor unless the bug fix clearly requires it.

Prefer fixes that reduce complexity at the same time as they fix correctness, such as collapsing a leaky helper into an owned module, removing a needless branch, or moving repeated sequencing into one place.

### Step 4: Re-run verification

Run the verification commands from the ExecPlan whenever they still apply. Add any targeted test or lint commands needed to validate the review fixes.

If verification cannot be run, say exactly why.

### Step 5: Summarize the pass

Report:

- what you changed
- what you validated
- whether the implementation achieved the intended complexity dividend or where it still falls short
- any remaining risk that was not appropriate to fix in this pass

Do not end with `continue`.

## Usefulness Scoring

Score the usefulness of the review-and-fix pass itself, not the absolute quality of the codebase.

Use this rubric:

- `9-10/10`: the review caught a meaningful correctness bug, regression, or missing validation that would likely have caused real trouble.
- `7-8/10`: the review found several substantive issues and materially hardened the implementation.
- `4-6/10`: the review made moderate but real improvements, such as tightening tests, validation, or edge-case handling.
- `1-3/10`: the review found little to improve beyond tiny polish, or confirmed the recent work was already in good shape.

The explanation must be concrete. Name the issue you fixed or explain why the pass was low-value.

## Output Contract

When the pass runs, end the response with:

`Usefulness score: X/10 - <specific reason>`

If no changes were needed, say so plainly and still end with the usefulness score. Never return `continue`.

If Step 0 short-circuits, return exactly `skip` and nothing else.

## Anti-Patterns

- Do not review unrelated old changes just because they are nearby.
- Do not invent problems to justify a higher score.
- Do not leave findings unfixed when the right change is obvious and safe.
- Do not replace verification with speculation.
- Do not ignore design regressions just because tests pass.

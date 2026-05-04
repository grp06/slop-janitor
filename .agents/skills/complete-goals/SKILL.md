---
name: complete-goals
description: >-
  Execute an approved .agent/goals plan through slop-janitor's goal runner,
  defaulting to .agent/goals/active when no explicit plan path is provided.
---

# Complete Goals

Execute an already approved durable goal plan. This skill is a launcher for the
repo-local runner; do not reimplement goal execution in the prompt.

## Workflow

1. Confirm the user is asking to execute an existing goal plan, not create or
   revise one.
2. Use the explicit plan path if the user provides one. Otherwise use
   `.agent/goals/active`.
3. If neither an explicit path nor `.agent/goals/active` exists, stop and ask
   the user to create or approve a plan with `create-goals`.
4. Run:

   ```bash
   slop-janitor goals run
   ```

   or, with an explicit plan:

   ```bash
   slop-janitor goals run .agent/goals/<id-slug>
   ```

5. If the runner reports that Codex experimental goals are disabled, report that
   warning and stop. Do not mutate `goals.json` by hand to force progress.
6. When the runner finishes, summarize completed goals, commands run, and any
   blocked goal from the updated `goals.json` and `ledger.jsonl`.

## Boundaries

- The artifacts under `.agent/goals/<id-slug>/` are the source of truth.
- The `slop-janitor goals run` command owns Codex thread goal setup, execution,
  ledger updates, checkpoint commits, and feature-gate warnings.
- Do not call Codex app-server goal APIs directly from this skill.
- Do not edit plan state except when the user explicitly asks to revise the plan.

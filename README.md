# `slop-janitor`

![slop-janitor](slop-janitor.png)

**Important: you must clone both this repo and the open-source Codex repo.** `slop-janitor` talks directly to Codex's app-server implementation, so it will not work with only this repository checked out.

`slop-janitor` automatically makes a repo cleaner, simpler, and more reliable.

Using Codex well usually means manually queuing a long chain of follow-up messages:

- ask Codex what the best refactor is
- ask it to improve that plan
- ask it to improve it again
- ask it to implement the plan
- ask it to review the result
- ask it to review it again with fresh eyes

`slop-janitor` runs that loop for you on one thread.

It follows the `PLANS.md` pattern from OpenAI's Codex exec plans guide: plan, improve the plan, implement, and review. That is the basic trick for keeping an agent on the same problem for a long time instead of resetting every turn. Background: [Codex Exec Plans](https://developers.openai.com/cookbook/articles/codex_exec_plans).

This tool uses the account you sign into Codex with for inference and token usage.

It also writes a complete run log, so the session is inspectable after the fact rather than something that only existed in the terminal.

By default, one cycle is:

1. `execplan-create`
2. `execplan-improve-subagents` x4
3. `implement-execplan`
4. `review-recent-work-subagents` x5

You can change the number of full cycles, improvement passes, and review passes.
You can also swap the follow-up skills with `--improve-skill` and `--review-skill`.

## Bundled Skills

The loop is built from a small set of repo-local skills in `.agents/skills`:

- `find-best-refactor`: finds the highest-leverage refactor.
- `execplan-create`: turns a prompt into an exec plan.
- `execplan-improve-subagents`: pressure-tests and rewrites that plan with a two-wave subagent audit.
- `implement-execplan`: implements the plan.
- `review-recent-work-subagents`: reviews the result with a two-wave subagent review-and-fix pass.

The most important step is `execplan-improve-subagents`. Fixing a weak plan is cheaper than fixing bugs later.

## Prerequisites

- Python 3.11 or newer.
- Rust and `cargo`.
- A separate clone of the open-source Codex repository.
- A Codex login.

The bundled skills used by `slop-janitor` live in `.agents/skills` inside this repository.

## Setup

Clone this repository and clone Codex separately:

```bash
git clone https://github.com/grp06/slop-janitor.git
git clone https://github.com/openai/codex.git
```

Point `slop-janitor` at the Codex Rust workspace:

```bash
export CODEX_WORKSPACE=/path/to/codex/codex-rs
```

You can also pass the path per command with `--codex-workspace /path/to/codex/codex-rs`.

Authenticate through the wrapped Codex login flow:

```bash
cd slop-janitor
./slop-janitor auth login
./slop-janitor auth login --device-auth
./slop-janitor auth status
./slop-janitor auth logout
```

The auth wrapper keeps stdin, stdout, and stderr attached to the terminal, so it behaves like native `codex login`. If your Codex access comes through ChatGPT, it will use that account. Details: [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan).

## Basic Use

The most natural use is refactor mode. Run it from the repository you want to improve:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --mode refactor
```

Add guidance if you want to steer the refactor:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --mode refactor --prompt "focus on testability and simplifying boundaries"
```

Run the default planning-first workflow if you want to start from an open-ended implementation prompt instead:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --prompt "help me build a CRM"
```

Increase the amount of iteration:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --prompt "help me build a CRM" --cycles 2 --improvements 5 --review 3
```

Use the non-subagent follow-up skills instead:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --mode refactor --improvements 5 --improve-skill execplan-improve --review 5 --review-skill review-recent-work
```

`slop-janitor` always targets the directory you launch it from, not the `slop-janitor` repository.

## Modes And Counts

`--mode pipeline` is the default. It requires `--prompt` and starts with `execplan-create`.

`--mode refactor` keeps the same follow-up structure, but replaces stage 1 with `find-best-refactor`. `--prompt` is optional in refactor mode. If you omit it, the first stage asks for the single highest-leverage refactor in the current repository.

`--cycles` controls how many times the full loop runs.

`--improvements` controls how many `execplan-improve-subagents` turns run inside each cycle.

`--review` controls how many `review-recent-work-subagents` turns run inside each cycle.

`--improve-skill` selects the planning-improvement skill for each improvement pass. Choices are `execplan-improve-subagents` and `execplan-improve`.

`--review-skill` selects the review skill for each review pass. Choices are `review-recent-work-subagents` and `review-recent-work`.

Defaults:

- `--cycles 1`
- `--improvements 4`
- `--improve-skill execplan-improve-subagents`
- `--review 5`
- `--review-skill review-recent-work-subagents`

When `--cycles` is greater than 1, stage labels in the run log are cycle-qualified, for example `cycle-2-execplan-create`.

## Codex Workspace Configuration

When `slop-janitor` launches the real Codex app-server or wrapped auth commands, it resolves the Codex workspace in this order:

1. `--codex-workspace /path/to/codex-rs`
2. `CODEX_WORKSPACE`

If neither is set, the command fails with a clear setup error.

Examples:

```bash
./slop-janitor --codex-workspace /path/to/codex/codex-rs --prompt "help me build a CRM"
./slop-janitor auth --codex-workspace /path/to/codex/codex-rs login
```

## What It Actually Does

Before stage 1, the client performs:

1. `initialize` with `capabilities.experimentalApi = true`
2. `initialized`
3. `account/read`
4. `thread/start`

If `account/read` says OpenAI auth is required and no account is logged in, the command fails immediately and tells you to run `./slop-janitor auth login`.

After that, every stage runs as a `turn/start` on the same thread. That is what gives the workflow continuity. The implementation and review stages see the plan that was just created and improved.

## Output Model

The terminal is intentionally sparse. During a run, it shows:

- agent-message commentary
- final agent-message text
- token usage

Everything else goes to the run log:

- stage banners
- command output
- file-change progress
- MCP progress
- item lifecycle notices
- failure details

Each run writes a full log to `runs/`. Log filenames start with the basename of the directory you launched from, followed by a UTC timestamp, for example `my-repo-20260317T213000Z.log`.

This split is deliberate. The terminal stays readable while the log remains complete.

## Reliability Contract

- `slop-janitor` requires a clean starting state in the primary repo and every linked repo it auto-manages. If any of them have pre-existing changes, it exits before stage 1 and tells you to commit, stash, or discard them first.
- Model settings are inherited from your current Codex config. `slop-janitor` overrides the thread `cwd`, forces `approvalPolicy: "never"`, and requests `workspace-write` sandboxing so planning and implementation stages can persist repo-local artifacts.
- The thread uses `approvalPolicy: "never"`.
- Auto-managed repos that start clean are required to stay clean at stage boundaries, except for the pending ExecPlan artifact in the primary repo while plan-improvement or implementation is in progress.
- Auto-managed repos are checkpointed after the final planning pass in a cycle, after `implement-execplan`, and after the final review pass in a cycle when those stages leave code changes behind.
- If the server asks for approvals, user input, permissions, MCP elicitation, or ChatGPT token refresh, `slop-janitor` responds deterministically, marks the stage failed, and exits after the matching `turn/completed`.
- Successful turns require real token data from `thread/tokenUsage/updated`. If a turn completes successfully without token usage, the run fails instead of printing invented zeros.
- Skill paths are validated before the app-server starts, so broken local setup fails early.

The tool is strict on purpose. When something is wrong, it should stop in a way you can diagnose.

## Tests

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

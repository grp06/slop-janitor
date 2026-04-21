# `slop-janitor`

![slop-janitor](slop-janitor.png)

**Important: you must clone both this repo and the open-source Codex repo.** `slop-janitor` talks directly to Codex's app-server implementation, so it will not work with only this repository checked out.

`slop-janitor` automatically makes a repo cleaner, simpler, and more reliable.

Using Codex well usually means manually queuing a long chain of follow-up messages:

- ask Codex for materially different refactor candidates
- ask it to pressure-test the shortlist and lock one refactor
- ask it to turn that decision into an exec plan
- ask it to improve the plan
- ask it to implement the plan
- ask it to review the result

`slop-janitor` runs that loop for you on one thread.

It follows the `PLANS.md` pattern from OpenAI's Codex exec plans guide: plan, improve the plan, implement, and review. That is the basic trick for keeping an agent on the same problem for a long time instead of resetting every turn. Background: [Codex Exec Plans](https://developers.openai.com/cookbook/articles/codex_exec_plans).

This tool uses the account you sign into Codex with for inference and token usage.

It also writes a complete run log, so the session is inspectable after the fact rather than something that only existed in the terminal.

By default, one cycle is:

1. `execplan-create`
2. `execplan-improve`
3. `implement-execplan`
4. `review-recent-work`

You can change the number of full cycles, improvement passes, and review passes.
You can also swap the follow-up skills with `--improve-skill` and `--review-skill`.

In `--mode refactor`, the cycle grows a three-stage front end:

1. `find-refactor-candidates`
2. `select-refactor`
3. `execplan-create`
4. `execplan-improve`
5. `implement-execplan`
6. `review-recent-work`

## Bundled Skills

The loop is built from a small set of repo-local skills in `.agents/skills`:

- `find-refactor-candidates`: searches the repo from first principles and writes a candidate shortlist into a work item.
- `select-refactor`: pressure-tests that shortlist and locks the winning refactor before planning starts.
- `execplan-create`: turns either a locked refactor decision or a raw prompt into an ExecPlan.
- `execplan-improve`: rewrites that plan with code-grounded corrections and missing details.
- `implement-execplan`: executes the active work-item ExecPlan while updating work-item state.
- `review-recent-work`: reviews the most recently implemented ExecPlan work and fixes obvious issues immediately.

Optional subagent variants for plan improvement and review are still bundled, but they are now follow-up choices rather than the default path.

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

Use the subagent follow-up skills instead:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --mode refactor --improvements 3 --improve-skill execplan-improve-subagents --review 2 --review-skill review-recent-work-subagents
```

Make sibling repos writable and auto-managed explicitly when one run needs to touch both:

```bash
cd /path/to/openclaw-cloud
/path/to/slop-janitor/slop-janitor --mode refactor --linked-repo /path/to/openclaw-studio-private
```

If you really want Codex to run without filesystem sandboxing, opt in explicitly:

```bash
cd /path/to/target-repo
/path/to/slop-janitor/slop-janitor --prompt "help me build a CRM" --sandbox danger-full-access
```

`slop-janitor` always targets the directory you launch it from, not the `slop-janitor` repository.

## Modes And Counts

`--mode pipeline` is the default. It requires `--prompt` and starts with `execplan-create`. In pipeline mode, `execplan-create` now prefers the work-item format under `.agent/work/<id-slug>/` and the follow-up skills keep operating on that active work item.

`--mode refactor` prepends three planning stages before the follow-up loop: `find-refactor-candidates`, `select-refactor`, and `execplan-create`. `--prompt` is optional in refactor mode. If you omit it, stage 1 asks for materially different refactor candidates in the current repository.

`--cycles` controls how many times the full loop runs.

`--improvements` controls how many plan-improvement turns run inside each cycle.

`--review` controls how many review turns run inside each cycle.

`--improve-skill` selects the planning-improvement skill for each improvement pass. Choices are `execplan-improve-subagents` and `execplan-improve`.

`--review-skill` selects the review skill for each review pass. Choices are `review-recent-work-subagents` and `review-recent-work`.

`--linked-repo /abs/path` adds another git repo to the managed run scope. Repeat it for more repos. These repos are checked for cleanliness, included in checkpoint commits, and added to the writable sandbox roots.

`--sandbox` controls the Codex filesystem sandbox. Choices are `workspace-write` and `danger-full-access`. The default is `workspace-write`.

`--stage-idle-timeout-seconds` controls how long a stage may go without any app-server activity before `slop-janitor` treats it as stuck and restarts recovery. The default is `900`.

`--max-stage-retries` controls how many retry attempts are allowed after the first failure for a single stage. The default is `6`.

`--retry-initial-delay-seconds` and `--retry-max-delay-seconds` control the capped exponential backoff between retry attempts. The defaults are `15` and `300`.

Defaults:

- `--cycles 1`
- `--improvements 1`
- `--improve-skill execplan-improve`
- `--review 1`
- `--review-skill review-recent-work`
- `--sandbox workspace-write`
- `--stage-idle-timeout-seconds 900`
- `--max-stage-retries 6`
- `--retry-initial-delay-seconds 15`
- `--retry-max-delay-seconds 300`

When `--cycles` is greater than 1, stage labels in the run log are cycle-qualified, for example `cycle-2-execplan-create`.

Prompt path detection is still supported as a convenience for linked repos, but explicit `--linked-repo` flags are the durable interface and avoid punctuation/parsing ambiguity.

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

After that, every stage in a cycle runs as a `turn/start` on the same thread. That is what gives the workflow continuity. The selection, planning, implementation, and review stages all see the same active work item and the same thread history for that cycle.

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

Each run also writes a machine-readable state file next to the log, using the same basename with a `.state.json` suffix. The state file tracks the current stage, retry attempt, thread id, and recovery status so long autonomous runs stay inspectable.

This split is deliberate. The terminal stays readable while the log remains complete.

## Reliability Contract

- `slop-janitor` requires a clean starting state in the primary repo and every linked repo it auto-manages. If any of them have pre-existing changes, it exits before stage 1 and tells you to commit, stash, or discard them first.
- Model settings are inherited from your current Codex config. `slop-janitor` overrides the thread `cwd`, forces `approvalPolicy: "never"`, and applies the selected sandbox mode for the whole run.
- In the default `workspace-write` sandbox, `slop-janitor` makes every managed repo root writable, not just the launch directory. The run log records the exact writable roots before stage 1.
- The thread uses `approvalPolicy: "never"`.
- Auto-managed repos that start clean are required to stay clean at stage boundaries, except for the workflow artifacts under `.agent/` in the primary repo while candidate selection, planning, or implementation is in progress.
- Auto-managed repos are checkpointed after the final planning pass in a cycle, after `implement-execplan`, and after the final review pass in a cycle when those stages leave code changes behind.
- Transient model-capacity failures such as `serverOverloaded` are retried automatically with capped exponential backoff.
- If a stage stops producing app-server activity, or the app-server process dies mid-stage, `slop-janitor` restarts the app-server and retries the current stage on a fresh thread.
- Before replaying a failed stage, `slop-janitor` compares the current workspace against a stage-start snapshot. If the stage appears to have partially changed repo state without satisfying a strong postcondition, the run stops instead of retrying blindly.
- If a cycle-start stage already refreshed its primary workflow artifact, or `implement-execplan` already marked the active work item as completed, `slop-janitor` treats that stage as completed and continues rather than replaying it.
- If the server asks for approvals, user input, permissions, MCP elicitation, or ChatGPT token refresh, `slop-janitor` responds deterministically, marks the stage failed, and exits after the matching `turn/completed`.
- Successful turns require real token data from `thread/tokenUsage/updated`. If a turn completes successfully without token usage, the run fails instead of printing invented zeros.
- Skill paths are validated before the app-server starts, so broken local setup fails early.

The tool is strict on purpose. When something is wrong, it should stop in a way you can diagnose.

## Tests

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

---
name: find-best-refactor
description: Find the single highest-leverage refactor in a repo by reducing system complexity, improving information hiding, deepening shallow modules, shrinking interface burden, and eliminating unnecessary special cases. Use when the user asks what to refactor, wants the best refactor, highest-leverage cleanup, architectural simplification, boundary extraction, duplication removal, complexity reduction, testability improvement, or an ExecPlan for the best refactor. Accept optional user guidance about scope, constraints, refactor style, or risk tolerance.
---

# Find Best Refactor

## Goal

Inspect the repo from first principles, generate multiple plausible refactor candidates, and pick the single refactor that removes the most complexity from the rest of the system.

The winning move is not the prettiest design or the most abstract decomposition. It is the change that most reduces what future readers and callers must understand.

Do not ask the user to choose among options. Make the call, explain it, and write an implementation-ready ExecPlan.

## Ousterhout Lens

Use John Ousterhout's design philosophy as the primary lens:

- prefer simple mental models over elegant-looking structure
- prefer deep modules over shallow wrappers
- prefer interfaces that hide sequencing and policy details
- prefer fewer concepts and fewer special cases
- prefer moving complexity behind a stable boundary over redistributing it

Treat these as the main forms of complexity:

- **Change amplification**: one logical change requires edits in many places
- **Cognitive load**: a reader or caller must hold too many facts in mind
- **Unknown unknowns**: important behavior is surprising, implicit, or scattered

The question you are answering is:
"What single refactor would most reduce the amount of system-specific complexity that the rest of this codebase must understand?"

## Mindset

Approach this like a principal engineer trying to reduce the long-term difficulty of the system.

You are not limited to one refactor shape. The winning move might be:

- deepening a shallow module that exposes too many implementation details
- hiding a multi-step sequence behind a simpler API
- collapsing parallel concepts into one owned abstraction
- removing a dead or thin abstraction layer
- extracting domain logic from infrastructure-heavy flows
- eliminating a special-case branch by normalizing the model
- merging duplicate orchestration paths that force readers to learn the same idea twice

Reject refactors that mainly move code around, add indirection, or create new concepts without hiding more detail.

## User Guidance Handling

The user may give extra guidance when invoking the skill. Use it, but interpret it correctly.

Treat guidance as one of two kinds:

- **Hard constraints** — explicit scope or prohibitions such as:
  - "only look in `app/auth`"
  - "do not propose schema changes"
  - "stay in the frontend"
  - "find the best low-risk refactor"
- **Soft guidance** — hints or priors such as:
  - "I think the API layer is messy"
  - "prefer testability wins"
  - "I suspect auth has duplication"
  - "look for something we could land quickly"

Rules:

- Honor hard constraints strictly.
- Treat soft guidance as a weighting signal, not as proof.
- If the repo-wide best candidate differs from the soft guidance, still make the best call within the guided scope if the user clearly wanted that scope. Briefly mention the distinction.
- If the user names a refactor type explicitly, evaluate that type first, but reject it if the evidence is weak and another interpretation is clearly better within the stated scope.

## Workflow

### Step 1: Establish Scope and Constraints

Determine scope from context. Default to the workspace root if unspecified.

Infer and state:

- target repo or directory
- hard constraints
- soft guidance
- risk tolerance
- whether the user wants the repo-wide best refactor or the best refactor inside a named area

Do not ask clarifying questions unless the guidance is directly contradictory.

### Step 2: Build a First-Principles Model of the Repo

Read the codebase systematically:

1. Start with `README`, `ARCHITECTURE.md`, or similar docs.
2. Identify languages, frameworks, major entry points, and the top 5-10 most referenced modules.
3. Map 3-5 core user-facing or business-critical flows.
4. Collect lightweight repo evidence before proposing anything:
   - import/reference frequency
   - file size and directory spread
   - change frequency from git history when available
   - co-change evidence for concepts that evolve together
   - whether tests exist near the affected code
   - whether the area is a stable core path or a niche edge path

For each important flow or module, ask:

- what does a caller need to know to use this correctly?
- which sequencing rules or policy choices are pushed onto callers?
- where are important decisions scattered across multiple files?
- where does one concept appear under multiple names or representations?
- where does the interface surface look large relative to the logic it hides?
- where do special cases multiply instead of being absorbed into the design?

Output a mental model:

- core concepts and file locations
- dependency highlights
- major flows
- evidence signals that suggest leverage or risk
- the most expensive sources of complexity in the current design

### Step 3: Generate Candidate Refactor Classes

Generate 2-5 candidates across refactor classes. Good candidates often come from these patterns:

- **Deepen a shallow module**
  - thin wrappers
  - leaky interfaces
  - APIs that require callers to coordinate too much
  - modules with many entry points but little hidden logic
- **Hide sequencing or policy**
  - multi-step flows repeated by callers
  - orchestration logic spread across handlers or services
  - policy checks duplicated across boundaries
- **Consolidate concepts**
  - duplicate abstractions
  - parallel hierarchies
  - config-driven duplication
  - duplicate state models
- **Eliminate special-case complexity**
  - branch-by-environment forks
  - exception-heavy flows
  - one-off conditionals that force readers to learn alternate rules
- **Remove a layer**
  - dead adapters
  - pass-through services
  - stale compatibility layers
  - abstractions whose callers still know implementation details

For each candidate, capture:

- candidate name
- refactor class
- files and flows involved
- what complexity exists today
- who pays that complexity cost today
- what knowledge would become hidden after the refactor
- why it would create a deeper module or simpler interface
- what evidence supports it
- what could make it a trap

Actively collect negative evidence too:

- what only looks duplicated but is intentionally separate
- what is ugly but already a clean boundary
- what would require a rewrite rather than a refactor
- what lies outside hard constraints

### Step 4: Score Candidates

Filter out any candidate that violates hard user constraints.

Score remaining candidates with this rubric:

| Criteria | Weight | Score (1-5) |
|----------|--------|-------------|
| Complexity removed from callers and readers | 25% | |
| Information-hiding gain | 20% | |
| Cognitive load reduction | 20% | |
| Change amplification reduction | 10% | |
| Special-case elimination | 10% | |
| Blast radius vs. risk | 5% | |
| Evidence confidence | 5% | |
| Ease of validation/rollback | 5% | |

Apply modifiers:

- small bonus for matching soft user guidance
- penalty for weak evidence
- penalty for hidden migration cost
- penalty for speculative architecture
- penalty when the candidate adds knobs, layers, or concepts
- penalty when the candidate mostly rearranges code without shrinking the interface burden
- penalty when the candidate sounds elegant but removes little real pain

The winner is the highest-scoring candidate after modifiers.

### Step 5: Make the Call

Present only the single best refactor.

Your answer should include:

1. **Current state** — what exists today and why it hurts
2. **Chosen refactor class** — deepen module, hide sequencing, consolidation, layer removal, special-case elimination, or similar
3. **Scope** — exact files and flows involved
4. **Why this is the best move** — evidence-based rationale
5. **Complexity dividend** — what callers or future readers will no longer need to know
6. **What stays untouched** — boundary of the change
7. **Implementation sketch** — target module shape, moves, import changes, and the first 1-2 tests to write or update
8. **Risks and mitigations** — realistic failure modes
9. **Why not the others** — 1-2 bullets per rejected candidate

If user guidance materially shaped the outcome, say how:

- "This is the best refactor within the frontend scope you requested."
- "Repo-wide, I would also consider X, but within your auth focus this is the best move."

### Step 6: Write the ExecPlan

After choosing the winner:

1. Use the `execplan-create` skill.
2. Read `{baseDir}/.agent/PLANS.md` in full.
3. If `{baseDir}/.agent/PLANS.md` is missing, follow the `execplan-create` fallback flow and use its bundled `PLANS.md`.
4. Write the plan to `.agent/execplan-pending.md`.
5. Make the plan implementation-ready: name specific files, describe the cut line, note the first safe slice to land, define validation steps, and include rollback notes.

The plan must explain:

- which complexity is being removed
- which interface or ownership boundary becomes simpler
- what knowledge moves from callers into the implementation
- how to validate that the new design is actually simpler in use, not just different internally

## Anti-Patterns to Avoid

- **Offering a menu** — Do not ask the user to pick from candidates.
- **Confusing scope guidance with proof** — A user hunch is a lead, not evidence.
- **Ignoring explicit user bounds** — If they said "frontend only," do not roam the backend.
- **Function-level nitpicking** — Focus on flow/module-level leverage, not tiny local cleanups.
- **Cosmetic refactors** — Renames and formatting are not winning moves.
- **Shallow abstractions** — Do not add layers that expose nearly as much complexity as they hide.
- **Boiling the ocean** — Avoid repo-wide rewrites disguised as refactors.
- **No evidence** — Every recommendation must cite files, flows, and concrete signals.
- **Complexity shifting** — If complexity just moves to callers, config, or coordination glue, it is not a win.
- **Vague plans** — If someone could not start implementation from the ExecPlan, the job is incomplete.

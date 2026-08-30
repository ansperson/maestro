# ADR-0011: Tool Evaluation Policy

Date: 2026-08-31

Status: Accepted

Extends: ADR-0002, ADR-0010

## Context

`AGENTS.md` decides when a workflow earns promotion to a Maestro tool: independent infrastructure
must provide material value, such as isolated execution, stable contracts, deterministic
validation, or observability. Today that judgement is made once, by argument, and never revisited.

Measurement against the fixture corpus showed why argument alone is not enough. Asked the same
questions about the same repository, the tool and a plain model invocation produced the same
conclusions at comparable cost. What the tool added was not a better answer: it was a typed
contract, evidence re-read against the repository, a fingerprint proving the investigation changed
nothing, and a durable Trail. A promotion argument that claims better answers would therefore have
been wrong, and nothing in the project would have caught it.

The same measurement showed how easily a single run misleads. One observation supported the
conclusion that a lower reasoning effort preserved quality; a later run at that setting returned
`uncertain` where a higher setting resolved. Conclusions drawn from unrepeated runs are not
evidence.

Comparing a tool against a plain model invocation is not symmetric. A tool returns a validated
structured result; a plain invocation returns prose. Scoring both with one rubric requires either
discarding the tool's structure or judging the prose.

## Decision

Every Maestro tool carries an evaluation with recorded ground truth. A tool without one is
incomplete, and this applies to deterministic tools as well as model-backed ones.

A tool whose behavior depends on model judgement additionally carries a control arm: the same
questions answered without the tool. Its purpose is to keep the promotion argument in
`AGENTS.md` falsifiable, by stating what the tool adds over the model that would otherwise
answer. A tool that wraps no model judgement has no meaningful control arm and carries none.

Comparison across output formats uses structured extraction, not holistic judging. A model may
convert prose into the same structured claims the tool returns, and both arms are then scored by
the same deterministic rubric against ground truth. A model asked instead to decide which answer
is better is rejected: it would make the verdict itself unreproducible, and a judge sharing a
model family with an arm it scores cannot be assumed impartial.

Verifiability is measured, never judged. Whether evidence resolves against the repository,
whether the fingerprint proves the repository was unchanged, and whether a Trail was recorded are
facts about an execution. They are the properties that justify a tool over a plain invocation, so
they are established deterministically and never delegated to a model.

An evaluation reports the variance of repeated runs and the cost of what it measured. A single
run is not a result, and a quality claim that ignores what it consumes is incomplete.

Evaluations remain outside the deterministic gate, as `AGENTS.md` already requires of AI-backed
suites. Their results inform promotion, retention, and configuration decisions rather than
blocking a merge on a stochastic outcome.

## Consequences

### Positive

- A promotion argument becomes falsifiable, and a tool that stops earning its place can be found.
- Cross-format comparison stays reproducible, because the verdict comes from a fixed rubric rather
  than from a model's opinion.
- The properties that actually distinguish a tool are established as facts, not as judgements.
- Prompt, model, and configuration changes acquire a measurable baseline.

### Negative

- Every new tool costs an evaluation corpus with real ground truth before it is complete.
- Repeated runs across two arms consume provider capacity, so corpora stay small and deliberate.
- The extraction step is itself model-dependent, and its reliability has to be tracked rather than
  assumed.
- Evaluations outside the gate can drift unnoticed unless they are run when the inputs they cover
  change.

## Rejected alternatives

- A model judging which answer is better: makes the verdict unreproducible, and a judge from the
  same model family as an arm cannot be assumed impartial.
- Scoring only the tool arm: the promotion argument stays unfalsifiable, which is the gap that
  produced this decision.
- Requiring a control arm for every tool: a tool that wraps no model judgement has nothing to
  compare against, so the requirement would be satisfied by an empty ritual.
- Evaluations as a merge gate: a stochastic result would either block honest work or be silenced,
  and `AGENTS.md` already separates AI-backed suites from the deterministic gate.
- Deferring repetition to a later revision: unrepeated runs already produced a wrong conclusion in
  this project, so a first evaluation without repetition would report confident noise.

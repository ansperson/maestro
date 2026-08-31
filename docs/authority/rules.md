# Delegated decision rules

* **Status:** Accepted

Writing a rule here delegates that class of decision to automation. There is no separate
delegation mechanism: the body of written rules *is* the delegation, and supervision is tuned
by editing this file rather than by changing code (ADR-0006).

Only entries inside the marked block below are authority. Everything else on this page is
context, including this paragraph. A rule states no approver, because writing it is the
delegation rather than an approval of one case.

A rule is matched exactly like a decision. It clears an action only when its scope reaches the
action and its validity still holds, so a rule cannot silently widen past what it names. A
rule that disagrees with a decision in force produces a conflict rather than a precedence, and
Maestro refuses and surfaces both.

## How to write one

```markdown
### Rule: <subject the rule settles>
- Decided: <the option that is delegated>
- Scope: project <name>          # or: work-item <reference>
- Validity: until superseded     # or: until YYYY-MM-DD
- Rationale: <optional, why this class is delegated>
```

Removing a rule narrows what runs unattended again, with no code change.

## Rules in force

<!-- maestro:rules:begin -->

### Rule: implementation.local_reversible
- Decided: agent-chosen
- Scope: project maestro
- Validity: until superseded
- Rationale: ADR-0006 keeps local reversible choices autonomous — helper extraction, internal data structures, test fixture organization — because turning every coding choice into a checkpoint is the cost this model exists to remove

<!-- maestro:rules:end -->

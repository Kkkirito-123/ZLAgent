---
name: code-review-quality
description: |
  Lightweight code-review checklist the agent runs before declaring a
  patch ready. Catches the obvious classes of issue (dead code,
  missing error handling, weakened tests) without a full security
  audit. Adapted from addyosmani's "Review" lifecycle phase.
version: 0.1.0
tags:
  - coding
  - quality
  - review
metadata:
  hermes:
    created_by: user
  zlagent:
    category: coding
    triggers:
      - review 一下
      - 检查一下代码
      - 代码有没有问题
      - code review
      - look for issues
    capabilities:
      - read_file
      - code_execution
    related_skills:
      - verification-before-completion
      - systematic-debugging
      - documentation-and-decisions
---

# code-review-quality

## Trigger

Activate after writing or editing code and BEFORE running the final
verification step. Also activate when the user pastes code and asks
for a review, or when the agent wants to gate a skill update.

## Principle

> Reviews catch what tests cannot see: intent drift, silent regressions,
> and code that technically works but will break the next editor.

## Inputs

- `changed_files`: the files / hunks to review.
- `purpose`: one-line statement of what the change should achieve.
- `reference`: original spec, issue, or plan the change maps to.

## Steps

1. **Intent check** — does each edit map back to a line in `purpose`?
   Flag hunks that drift.
2. **Scope creep** — unrelated renames / formatting mixed into a
   focused fix? Split if non-trivial.
3. **Error handling** — new call sites to I/O / HTTP / subprocess have
   explicit error paths (not just `except Exception: pass`)?
4. **Tests** — did any existing assertion weaken (broader tolerance,
   deleted edge case)? Restore or justify.
5. **Invariants** — was a class invariant (e.g. "every tool row has a
   permission tier") maintained?
6. **Dead paths** — code added that is never reachable from the
   feature's entry point? Drop.
7. **Logging / observability** — significant state change logged? On
   the happy path only, or also on failure?
8. **Names** — any new symbol that will be misleading in 3 months?
   Rename if yes, keep if no.
9. **Non-goal check** — the change should NOT accidentally handle
   something out of scope (e.g. a skill refactor that changes memory
   policy).

## Output Format

Grade each finding as:

- 🟥 **must-fix**: blocks merge.
- 🟧 **should-fix**: merge allowed but tracked.
- 🟩 **nit**: optional polish.

```
### code-review-quality findings

- 🟥 _<file>:<line>_  <one-line issue>  — _why it matters_
- 🟧 ...
- 🟩 ...

(零 must-fix 时写 "✅ no blocking issues found")
```

## Verification

- Every finding cites `<file>:<line>`.
- Must-fix items are addressed before declaring done.
- No finding is vague ("looks weird") — each has a concrete trigger.

## Failure Signals

- 0 findings on a 300-line diff: review was rubber-stamped.
- "nit" storm with no must-fix on a patch that later breaks: the
  gradient is wrong — recalibrate severity thresholds.
- The same class of issue appears repeatedly across reviews: promote
  it to a guardrail (skill, hook, or lint rule).

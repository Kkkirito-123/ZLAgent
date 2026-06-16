---
name: documentation-and-decisions
description: |
  Produce concise docs and Architectural Decision Records (ADRs) when
  a workflow or design becomes durable. Favours short artifacts over
  exhaustive manuals. Adapted from addyosmani's "documentation-and-adrs"
  and the skill-authoring doctrine.
version: 0.1.0
tags:
  - meta
  - docs
  - adr
metadata:
  hermes:
    created_by: user
  zlagent:
    category: meta
    triggers:
      - 写个文档
      - 记录一下决定
      - ADR
      - 写 README
      - document this
      - write a decision record
    capabilities:
      - write_file
      - skill_manage
    related_skills:
      - skill-authoring
      - task-planning
      - code-review-quality
---

# documentation-and-decisions

## Trigger

Activate when:

- A recurring workflow is about to be repeated a third time — promote
  it to a skill (via `skill-authoring`) OR to a doc.
- A design decision is made that future sessions would want to know
  ("we chose SQLite over Redis for this because...").
- The user explicitly asks for a README / ADR / decision record.
- A feature lands and the deployment behaviour would surprise a new
  operator.

## Principle

> Short, lived docs > long, stale docs. Write the minimum that future
> you would thank present you for. Delete anything that's wrong.

## Doc Shapes (pick one)

1. **README section** — goal, usage, gotchas. Max ~60 lines.
2. **ADR (Architectural Decision Record)** — Context, Decision,
   Alternatives, Consequences. Max ~40 lines. File name
   `docs/adr/YYYY-NN-<slug>.md`.
3. **Runbook entry** — "when X happens, do Y; if Y fails, Z".
   Max ~30 lines. File name `docs/runbooks/<area>.md`.
4. **Skill docstring update** — inline inside an existing `SKILL.md`
   when the doc IS the workflow.

## Inputs

- `target`: what we're documenting (feature, decision, workflow).
- `audience`: future-self, teammates, operator.
- `shape`: which of the four shapes above.

## Steps

1. Pick the shape from the list above — don't invent a new format.
2. For ADRs, state **what was considered and rejected**. A one-line
   "we chose X" is not an ADR.
3. Name concrete commands / file paths / links. No "see the diagram"
   without a diagram.
4. Include a "revisit when..." trigger so the doc has a life-cycle
   signal ("revisit when the SQLite row count > 100k").
5. Write via `write_file` to the correct path; `skill_manage` for
   embedded skill docs.

## ADR Template

```
# ADR <NN>: <Short decision title>
Date: YYYY-MM-DD

## Context
<2-4 sentences: what pushed us into this decision>

## Decision
<1-3 sentences: what we chose>

## Alternatives considered
- <A>: <why rejected>
- <B>: <why rejected>

## Consequences
- Positive: <...>
- Negative: <...>

## Revisit when
<a concrete signal>
```

## Verification

- The doc is under 100 lines unless it's a reference manual.
- Every code / command snippet actually runs on the project as of
  today.
- The "revisit when" trigger is concrete, not "later".
- Links resolve (check via `read_url` if external).

## Failure Signals

- "This document is out of date" comments added and left forever —
  delete the stale section instead.
- ADR with no "Alternatives considered" — it's a status report, not a
  decision record.
- The same decision keeps being re-discussed: the ADR was never read.
  Link to it from the code / skill it governs.

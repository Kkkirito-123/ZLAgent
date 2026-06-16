---
name: systematic-debugging
description: |
  Root-cause-first debugging loop for code or runtime errors: understand
  the failure before proposing a fix. Adapted from Hermes' systematic-
  debugging — same iron law, smaller surface, ZLAgent-tool-aware.
version: 0.1.0
tags:
  - coding
  - debugging
metadata:
  zlagent:
    category: coding
    triggers:
      - 报错
      - 这个 bug
      - 修一下
      - debug
      - 调试
      - root cause
      - traceback
      - exception
    capabilities: []
    related_skills:
      - task-planning
      - skill-authoring
---

# systematic-debugging

## Trigger

Use this skill when the user reports a bug, a failing test, an
unexplained runtime exception, a flaky job, or pastes a traceback.

Do NOT use this when:
- The user only wants a code review of working code.
- The error is obviously environmental (missing env var) and the fix
  is one line.
- The user explicitly says "just patch it, don't investigate".

## The Iron Law

**No fix without a root cause.** A symptom fix is failure. If you
haven't completed Phase 1, you cannot propose a fix.

## Inputs

- `symptom`: the user-visible failure (error message, wrong output,
  hang, ...). Capture verbatim.
- `repro`: how to reproduce. Ask if missing.
- `recent_changes`: what changed since it last worked.
- `scope`: file paths / functions / cron jobs / channels involved.

## Steps

Phase 1 — Understand:
1. Restate the symptom in your own words. If you can't, ask.
2. Reproduce or get the user to reproduce. No repro = no fix.
3. Read the smallest necessary code with `read_file`. Avoid wide grep.
4. State the hypothesis as: "Failure happens because X, evidenced by Y".

Phase 2 — Localise:
5. Add minimal logging or a targeted test that would prove / disprove
   the hypothesis.
6. Run it (or have the user run it) and read the new evidence.

Phase 3 — Fix:
7. Propose the smallest change that addresses the root cause. Show the
   diff. State which assumption it corrects.
8. Verify with the same repro / test you used in Phase 2.

Phase 4 — Land:
9. State what regression test was added (or why none was needed).
10. Note any related-but-not-fixed issues; do not silently widen scope.

## Verification

- The fix reverts a specific named cause, not a generic "improvement".
- The repro that failed before the fix passes after.
- Tests that already passed still pass.
- Changes are minimal — no drive-by edits, no comment churn.

## Failure Signals

- "Try this and see if it works" without a hypothesis.
- Multiple unrelated changes in one fix.
- The user has to debug your fix.
- Same bug class re-fires within a week.

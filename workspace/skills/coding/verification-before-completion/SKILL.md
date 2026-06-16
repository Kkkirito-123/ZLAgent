---
name: verification-before-completion
description: |
  Do not declare a task done without running verification. For code,
  that means running tests / smoke; for answers, that means checking
  the claim against a fresh source. Adapted from obra/superpowers and
  addyosmani's "Verify" lifecycle phase.
version: 0.1.0
tags:
  - coding
  - quality
  - meta
metadata:
  hermes:
    created_by: user
  zlagent:
    category: coding
    triggers:
      - 验证一下
      - 跑一下测试
      - 确认一下
      - 真的修好了吗
      - verify
      - confirm it works
    capabilities:
      - code_execution
      - test
    related_skills:
      - systematic-debugging
      - code-review-quality
      - task-planning
---

# verification-before-completion

## Trigger

Activate before saying "done", "fixed", "完成了", "修好了", or shipping
any code change to the user. Also activate at the end of every task
from `task-planning`.

## Principle

> A claim of completion without verification is just optimism. Every
> deliverable needs a concrete check that would have caught the bug.

## Inputs

- `task_description`: what was being done.
- `artifacts`: files edited, commands run, tools called.
- `risk_class`: `"code" | "data" | "info" | "config"` — dictates the
  verification depth.

## Steps

1. **Name the check up front** (before writing the code) when possible:
   "I'll know this is fixed when X."
2. **For code (`code` class):**
   - Compile / type-check when available (e.g. `python -m compileall`).
   - Run the relevant test or smoke suite.
   - If no suitable test exists, author ONE small targeted test.
   - Verify the output / exit code matches the expectation.
3. **For data transformations (`data` class):**
   - Spot-check 2-3 representative rows before and after.
   - Confirm row counts match expected, or explain the delta.
4. **For factual claims (`info` class):**
   - Re-check via an independent fresh `read_url` or `web_search`.
   - Never declare certainty from a single source.
5. **For config / runtime changes (`config` class):**
   - Boot / health-check the affected service.
   - Exercise the one path the change touches.
6. Summarize: what was verified, by what command/check, with what
   result.

## Verification

- The step that would have caught the bug actually ran.
- The exit code / test result / fresh source is cited explicitly.
- "Done" is never claimed based on "it should work" or "looks right".

## Failure Signals

- "Done" with no command output or test pass reference.
- Smoke / compileall skipped because "small change".
- Verification ran against stale cached data.
- The user later reports a regression the verification step would have
  caught — audit and tighten the check list.

## Output Format

When reporting completion, always include a short verified block:

```
✅ 完成：<一句话结果>
验证：
  - <check 1>  → <result>
  - <check 2>  → <result>
```

If any check failed, DO NOT declare done — go back to
`systematic-debugging`.

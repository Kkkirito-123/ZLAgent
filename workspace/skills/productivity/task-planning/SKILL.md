---
name: task-planning
description: |
  Reply with a structured plan instead of executing. Goal, assumptions,
  numbered steps, verification, risks. No file writes unless the user
  explicitly asks. Used when the user says "先列个计划" / "先想想再做".
version: 0.1.0
tags:
  - productivity
  - planning
metadata:
  zlagent:
    category: productivity
    triggers:
      - 列个计划
      - 先想想
      - 先想清楚
      - 给我一个方案
      - 不要直接动手
      - plan first
      - draft a plan
      - dont execute yet
    capabilities: []
    related_skills:
      - systematic-debugging
      - skill-authoring
---

# task-planning

## Trigger

Use this skill when the user wants a plan rather than execution. Common
phrases: "先列个计划", "不用马上做", "你先想清楚", "draft a plan", or
explicitly "plan mode".

Do NOT use this when:
- The task is a simple reminder / lookup → just do it.
- The user only wants the answer, not the plan.
- The user already has a plan and asks you to start executing.

## Inputs

- `goal`: what the user wants to end up with. If unclear, ask once.
- `constraints`: time, tools allowed, files allowed to touch.
- `assumptions_known`: anything the user already gave you.
- `output`: default `chat` (reply in IM); only write a plan file when
  the user explicitly says "保存到 X" or "写进文档".

## Steps

1. Restate the goal in one line. If unsure, ask one targeted question
   instead of guessing.
2. Surface the assumptions you are making. Mark unknowns clearly.
3. Break the work into bite-sized numbered steps. Each step must be:
   - independently checkable
   - small enough that a junior could do it
   - paired with how you'd verify it (compile / test / smoke / manual)
4. List risks and rollback options for any step that touches state.
5. End with: "要我按这个计划执行吗？" — never start executing in the
   same turn.

## Verification

- The plan covers goal, assumptions, steps, verification, risks.
- No step is "and then it's done" — every step has a check.
- The reply is IM-friendly: numbered list, short lines, no walls.
- The skill never proactively writes files unless `output != chat`.

## Failure Signals

- Plan reads like an essay.
- Steps are vague ("research the problem", "make it work").
- Verification missing on state-changing steps.
- The agent silently executed instead of proposing the plan.

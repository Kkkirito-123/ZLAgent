---
name: using-skills
description: |
  Meta skill that guides the agent on when to pick an existing skill,
  when to create a new one, and when to install an MCP server. Adapted
  from obra/superpowers' "using-superpowers" and addyosmani's "meta"
  skill for personal-AI use.
version: 0.1.0
tags:
  - meta
  - skills
metadata:
  hermes:
    created_by: user
  zlagent:
    category: meta
    triggers:
      - 有哪些技能
      - 你会什么
      - 用哪个技能
      - 怎么做这个
      - which skill
      - what can you do
    capabilities:
      - skill_routing
    related_skills:
      - skill-authoring
      - mcp-discovery
      - skill-vetter
---

# using-skills

## Trigger

Pick this skill when the user asks what you can do, which skill applies
to their task, or when the right move is unclear. Also activate
internally before starting any non-trivial task so the agent does a
one-pass "do I already have a skill for this?" check.

## Principle

> Use the right skill for the task; install a new capability **only**
> when none of the installed options fit.

Core capabilities are always available but hidden from the user view.
User-installed extensions surface through `/list`.

## Decision Tree

1. **Does an installed skill match the intent?**
   - Yes → route the turn through that skill. Done.
   - No → step 2.

2. **Can the task be done with existing tools** (`read_url`,
   `web_search`, `code_execution`, `read_file`, etc.) without a new
   skill?
   - Yes → run it directly, no new skill needed.
   - No → step 3.

3. **Is the workflow durable** (will it repeat across sessions / days)?
   - Yes → propose to author a new skill via the `skill-authoring`
     skill. Confirm with the user before creating.
   - No → one-off execution with existing tools.

4. **Does the task need an external service** the agent cannot reach
   with current tools (e.g. GitHub API, Notion, a video download
   service)?
   - Yes → route through the `mcp-discovery` skill: search the MCP
     ecosystem, propose ONE candidate, install after confirmation.
   - No → the task is probably out of scope; say so plainly.

## Inputs

- `user_intent`: a short summary of what the user is asking for.
- `current_inventory`: the harness inventory (what skills / MCP /
  plugins are installed). Fetch via `/api/harness/extensions` or by
  calling `tool_search` when browsing built-in tools.

## Steps

1. Summarize user intent in one line.
2. Walk the decision tree above.
3. If creating or installing, confirm with the user first — never
   install without a yes/no.
4. Record the decision briefly if the path is non-obvious ("chose
   skill X because Y").

## Verification

- The routed / suggested skill actually matches the intent.
- No new skill is created when an existing one would fit.
- No MCP is installed without user confirmation.
- For skill creation, run `skill-authoring` rather than hand-writing
  frontmatter.

## Failure Signals

- The same one-off task spawns a new skill (skill inflation).
- A skill matches by name but not by workflow, causing wrong results.
- The user says "you already have that" — signals a missed inventory
  check.
- Multiple MCP installs proposed in one turn — too aggressive.

## Output Format

When the user asks "what can you do", reply with a compact 3-5 line
summary grounded in the live inventory, not a hardcoded list. Mention
core capabilities exist without enumerating them:

```
当前可用：<N> 个已安装的用户技能 / <M> 个 MCP 服务器 / <K> 个插件。
常见场景：<一句话举例>。
核心能力（内置隐藏）：旅行、天气、写作、调试、规划、研究等。
需要新功能？告诉我做什么，我去找合适的 skill 或 MCP。
```

---
name: skill-authoring
description: |
  Author a new ZLAgent skill in the canonical Trigger / Inputs / Steps /
  Verification / Failure Signals format. Class-level only. Used when the
  user says "把这个流程记成 skill" or in background skill review.
version: 0.1.0
tags:
  - meta
  - skills
metadata:
  zlagent:
    category: meta
    triggers:
      - 记成 skill
      - 写一个技能
      - 保存为 skill
      - author skill
      - new skill
    capabilities: []
    related_skills:
      - task-planning
      - systematic-debugging
---

# skill-authoring

> 一切 skill 都应当遵循钱学森《论系统工程》的"目标—输入—执行—输出—反馈"
> 控制论闭环。本 skill 教 agent 把这个框架套进 SKILL.md，
> 模板原型见 `workspace/skills/_TEMPLATE.md`。

## 一、目标 / Goal

把一个**类级别**（class-level，至少 2 次以上可复用）的工作流固化成
SKILL.md，让未来同类任务可以一次命中、一次执行、一次校验。

不属于本 skill 的范围：
- 一次性任务 ("我今天加班别忘了") → 走 memory
- 单一 bug 修复 ("OpenAI 限速 2026-05-09") → 也走 memory
- 已有 skill 暴露了 pitfall（一两行就能补） → 用 `skill_manage action='patch'`

## 二、触发 / Trigger（输入信号）

正向触发：
- 用户显式说"把这个流程记成 skill / 保存为 skill / new skill"
- agent 在 background review 时判定该轮次产出值得固化
- 某个已有 skill 需要**结构性重写**才能匹配 五要素 模板

反向排除：见上文"目标"小节末尾的边界条件。

## 三、步骤 / Steps（执行控制）

1. **校验 class-level**：至少能想到 2 个同形未来任务；想不到就拒绝创建。
2. **填 frontmatter**——只填必要字段，其它留空：
   ```yaml
   name: <kebab-case-name>
   description: |
     一句话说明触发场景 + 可观测产出。≤200 字符。
   version: 0.1.0
   tags: [<category>, ...]
   metadata:
     zlagent:
       category: <category>
       triggers: [...]
       capabilities: []
       related_skills: [...]
   ```
3. **写 body 五要素**——节标题用双语、按下列固定顺序，缺一不可：
   - `## 一、目标 / Goal`
   - `## 二、触发 / Trigger（输入信号）`
   - `## 三、步骤 / Steps（执行控制）`
   - `## 四、输出 / Output（被控变量）`
   - `## 五、反馈 / Feedback（偏差检测 + 校正）`
4. **保持总长 ≤120 行（≤2 KiB）**；超出就把素材拆到 `references/`。
5. **调用 `skill_manage(action='create', ...)`**。
   `skill_body` 不要包含外层 YAML `---` 分隔符——`skill_manage` 自己渲染。

> 涉及代码 / 调试 / 重构 / 实现方案时遵循 Karpathy-inspired 原则：
> 编码前思考、简洁优先、精准修改、目标驱动执行。

## 四、输出 / Output（被控变量）

`skill_manage` 创建成功的 skill 应当满足：
- frontmatter 是合法 YAML，含 `name` + `description`。
- body 五个章节齐全，标题字面与上文步骤 3 一致。
- 任何在"步骤"里被引用的 tool 都真实存在于 registry（必要时先 `tool_search`）。
- triggers 与现有 skill 的 triggers 重叠 ≤30%；重叠多的话本应 patch 不是 create。

## 五、反馈 / Feedback（偏差检测 + 校正）

偏差信号——出现任一项就立即停手，不要把这种 skill 写进 workspace：
- skill body 读起来像聊天记录而不是受控过程
- "步骤"写成散文，读完不知道"具体要做哪一步"
- triggers 含本次任务的特定实体（日期、人名、URL、bug 编号）
- skill 行数 ≥200 还没拆 `references/`
- 已有同义 skill，正确动作是 `patch` 而不是 `create`

校正策略：
- body 读起来像聊天 → 重写成"动作 + 校验点"的最小步骤序列
- 触发太具体 → 抽到类级别（"每周 X 类"而非"本周二的事"）
- 与现有 skill 重叠 → 改调 `skill_manage(action='patch', ...)`
- 模板要素缺失 → 回退到 `workspace/skills/_TEMPLATE.md` 重新对齐

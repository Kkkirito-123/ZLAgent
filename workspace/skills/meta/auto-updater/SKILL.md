---
name: auto-updater
description: |
  定期检查 ZLAgent 的 skill、MCP 工具、记忆摘要的变化，
  生成一份变更摘要并通过 IM 发送给用户。
version: 0.1.0
tags:
  - meta
  - automation
  - cron
metadata:
  zlagent:
    category: meta
    triggers:
      - 每天发摘要
      - 自动更新报告
      - 有什么新变化
      - 发一下变更日志
      - 每周更新
      - auto update
    capabilities:
      - memory_manage
      - skill_manage
      - send_message
    related_skills:
      - skill-authoring
      - news-digest
---

# auto-updater

## 一、目标 / Goal

让 agent 主动感知自身的变化（skill 增删、记忆更新、工具变更），
并定期给用户发一份"这段时间发生了什么变化"的摘要。

核心价值：用户不用手动问，agent 自己报告自己的成长轨迹。

不属于本 skill 的范围：
- 检查外部新闻/repo 更新 → 走 news-digest / arxiv
- 检查 ZLAgent 源代码更新 → 需要 git MCP，不在本 skill 范围

## 二、触发 / Trigger（输入信号）

两种触发方式：

**主动触发（推荐用 cron_manage 设置定时）**：
- 每天早上 9 点运行一次
- 典型 cron 表达式：`0 9 * * *`

**被动触发（用户询问）**：
- "这周有什么变化"
- "发一下变更日志"
- "最近 agent 学了什么"

## 三、步骤 / Steps（执行控制）

1. **收集 skill 变更**：
   - 调用 `skill_manage(action='list')` 获取当前 skill 列表
   - 与上次运行时记录的列表对比（从 memory 里读取 `auto_updater_last_skills`）
   - 记录新增 / 删除的 skill 名称

2. **收集记忆变化**：
   - 调用 `memory_manage(action='list')` 获取当前记忆条目
   - 与上次记录对比（`auto_updater_last_memory_count`）
   - 只记录条数变化，不暴露记忆内容

3. **收集工具变化**（可选）：
   - 如果本次运行发现有新 MCP 工具，记录工具名

4. **生成摘要**，格式见 Output。

5. **更新 memory 快照**：
   - `memory_manage(action='remember', content='auto_updater_last_skills: [...]')`
   - `memory_manage(action='remember', content='auto_updater_last_memory_count: N')`

6. **发送摘要**：
   - 如果由 cron 触发：通过 `send_message` 发给用户
   - 如果由用户询问：直接在当前对话回复

## 四、输出 / Output（被控变量）

摘要格式（IM 友好，控制在 300 字以内）：

```
📊 ZLAgent 周报 · <日期>

🧠 技能变化
  + 新增：humanizer, skill-vetter（共 +2）
  - 删除：（无）

💾 记忆变化
  本周新增记忆 5 条，当前共 23 条

🔧 工具变化
  （无变化）

下次报告：明天 09:00
```

如果什么都没变化，发：
```
📊 ZLAgent 日报 · <日期>
本日无变化。一切正常运行。
```

## 五、反馈 / Feedback（偏差检测 + 校正）

偏差信号：
- memory 读取失败 → 报告"无法读取上次快照，本次作为基准记录"然后继续
- skill_manage 返回空 → 报告并停止，不发送空摘要
- send_message 失败 → 把摘要写入 memory 作为备份

注意：
- 不要在摘要里暴露记忆的具体内容，只报告数量变化
- 如果用户问"记忆里有什么" → 那是 `memory_manage` 的工作，不是本 skill

# re_zlagent 贡献指南

`re_zlagent` 是新的 ZLAgent 重构工作区。所有贡献都应保持小范围、可验证，并与 `CLAUDE.md` 的架构规则一致。

AI contributor 只使用英文权威文件：`AGENTS.md`、`CLAUDE.md`，以及阶段开发时的 `ROADMAP.md`。所有 `*.zh-CN.md` 都是同步的用户阅读翻译，不是工程执行权威。

## 工作流程

1. 阅读 `AGENTS.md`。
2. 阅读 `CLAUDE.md`。
3. 涉及阶段开发或迁移决策时，阅读英文 `ROADMAP.md`。
4. 改代码前先检查现有实现、测试和文档。
5. 涉及重构、删除、新功能、依赖变更或数据库结构变更时，先写中文计划并等待确认。
6. 只实现已经确认的最小切片，不能提前引入后续路线图阶段。
7. 运行合适的测试或检查命令。
8. 英文权威文档和对应中文用户翻译必须一起更新。
9. 最终报告需要说明改动文件、验证方式、风险和可沉淀的新规则。

## 计划格式

每个迁移或开发计划必须包含：

- 本次目标
- 用户 / 干系人
- MVP 范围
- 不做什么
- 预计文件结构或改动范围
- 验收标准
- 风险与取舍

## 默认验证命令

```bash
python -m pip install -e 're_zlagent[dev]'
PYTHONPATH=re_zlagent/src python -m re_zlagent.check
PYTHONPATH=re_zlagent/src python -m unittest discover -s re_zlagent/tests -t re_zlagent
PYTHONPATH=re_zlagent/src python -m re_zlagent.benchmark --pretty
ruff check re_zlagent/src re_zlagent/tests
mypy re_zlagent/src/re_zlagent
python -m compileall re_zlagent/src re_zlagent/tests
PYTHONPATH=re_zlagent/src python -m re_zlagent.app.cli --help
```

改动范围较小时可以运行定向测试，但不能用局部测试声称完成了全量验证。

## 复盘格式

最终回复应回答：

- 改了什么
- 改了哪些文件
- 如何验证
- 发现的问题
- 剩余风险
- 可沉淀的新规则

# Contributing to re_zlagent

`re_zlagent` is a rebuild workspace. Keep contributions small, verifiable, and aligned with `CLAUDE.md`.

## Workflow

1. Read `AGENTS.md`.
2. Read `CLAUDE.md`.
3. Inspect existing implementation and tests before changing code.
4. For refactors, deletions, new features, dependency changes, or database changes, write a Chinese plan and wait for confirmation.
5. Implement the smallest approved slice.
6. Run the right tests.
7. Report changed files, validation, risks, and new rules worth keeping.

## Plan Format

Every migration plan must include:

- 本次目标
- 用户 / 干系人
- MVP 范围
- 不做什么
- 预计文件结构或改动范围
- 验收标准
- 风险与取舍

## Validation

Default commands:

```bash
PYTHONPATH=re_zlagent/src python -m re_zlagent.check --skip-package
python -m unittest discover -s re_zlagent/tests
python -m compileall re_zlagent/src re_zlagent/tests
PYTHONPATH=re_zlagent/src python -m re_zlagent.app.cli --help
```

Use targeted tests when the change is narrow, but do not claim broad validation from narrow checks.

## Review Expectations

Every final report should answer:

- 改了什么
- 改了哪些文件
- 如何验证
- 发现的问题
- 剩余风险
- 可沉淀的新规则

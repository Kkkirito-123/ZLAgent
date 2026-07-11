# Contributing to re_zlagent

`re_zlagent` is a rebuild workspace. Keep contributions small, verifiable, and aligned with `CLAUDE.md`.

AI contributors use English authority files only: `AGENTS.md`, `CLAUDE.md`, and,
for staged work, `ROADMAP.md`. Files ending in `.zh-CN.md` are synchronized user
translations and are not operational authority.

## Workflow

1. Read `AGENTS.md`.
2. Read `CLAUDE.md`.
3. Read `ROADMAP.md` when the work changes a delivery stage or migration decision.
4. Inspect existing implementation and tests before changing code.
5. For refactors, deletions, new features, dependency changes, or database changes, write a Chinese plan and wait for confirmation.
6. Implement the smallest approved slice without pulling later roadmap stages forward.
7. Run the right tests.
8. Update English authority docs and their Chinese user translations together.
9. Report changed files, validation, risks, and new rules worth keeping.

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
python -m pip install -e 're_zlagent[dev]'
PYTHONPATH=re_zlagent/src python -m re_zlagent.check
PYTHONPATH=re_zlagent/src python -m unittest discover -s re_zlagent/tests -t re_zlagent
PYTHONPATH=re_zlagent/src python -m re_zlagent.benchmark --pretty
ruff check re_zlagent/src re_zlagent/tests
mypy re_zlagent/src/re_zlagent
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

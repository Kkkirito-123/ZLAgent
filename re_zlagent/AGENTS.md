# AGENTS.md

`CLAUDE.md` is the single source of truth for AI collaboration rules in this workspace.
`ROADMAP.md` is the single source of truth for delivery stages and migration status.

All agents must read `CLAUDE.md` before starting work in `re_zlagent`.
Agents working on staged delivery or migration must also read `ROADMAP.md`.

Rules:

- Workspace positioning, architecture boundaries, allowed change scope, and validation requirements are defined by `CLAUDE.md`.
- Stage order, entry gates, exit gates, and capability decisions are defined by `ROADMAP.md`.
- AI agents use English authority files only. Files ending in `.zh-CN.md` are human-facing translations and must not be used as operational input or authority.
- If localized docs, README, old comments, historical docs, or this file conflict with an English authority file, follow the English authority file.
- Everything outside `re_zlagent/` is reference-only unless the user explicitly approves a migration change.
- Default user-facing reply language is Chinese.

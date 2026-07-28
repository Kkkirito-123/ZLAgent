# Attributions and Source Register / 来源与归属清单

ZLAgent 的原创实现与文字采用 [MIT License](LICENSE)。本清单记录仓库中保留、改写或
仅作兼容性与设计参考的外部材料；它不会重新许可任何第三方作品，也不表示上游作者对
本项目的认可。

Apart from standard license notices reproduced for their intended purpose and
the Vibe Coding Rules-derived workflow package identified below, no third-party
source code, image, or substantial expressive passage is vendored in this
repository. Unless an entry says otherwise, the relationship is reference-only.

## Retained and adapted material / 保留与改写材料

### Vibe Coding Rules

- Source: [Kkkirito-123/Vibe-Coding-Rules at `4ec4e566ecea`](https://github.com/Kkkirito-123/Vibe-Coding-Rules/tree/4ec4e566eceac6e6f2c149d8efb6d8a352ac094f).
- Use: repository guides, `.agents/skills/` workflows, rule validation, and the
  evidence-based delivery conventions were adapted to ZLAgent's real product
  boundaries.
- License: [MIT](https://github.com/Kkkirito-123/Vibe-Coding-Rules/blob/4ec4e566eceac6e6f2c149d8efb6d8a352ac094f/LICENSE).
- Status: modified and retained, not reference-only. ZLAgent-specific product,
  architecture, command, and support claims replace template placeholders.

## Documentation and interoperability references / 文档与兼容性参考

### Agent instruction and Skill formats

- Sources: [OpenAI AGENTS.md guidance](https://developers.openai.com/codex/guides/agents-md),
  [Codex Skills](https://developers.openai.com/codex/skills),
  [OpenAI Skill Creator at `49f948faa925`](https://github.com/openai/skills/tree/49f948faa9258a0c61caceaf225e179651397431/skills/.system/skill-creator),
  [Claude Code project memory](https://code.claude.com/docs/en/memory), and the
  [Agent Skills specification](https://agentskills.io/specification).
- Use: instruction discovery, thin `CLAUDE.md` import, Skill frontmatter,
  metadata field names, routing, and progressive disclosure.
- License status: the OpenAI Skill Creator files and Agent Skills repository
  use Apache-2.0 at the audited revisions; public product documentation is
  treated as reference-only.
- Status: compatible formats and independently authored project content; no
  upstream implementation or documentation passage is included.

## Automation dependencies / 自动化依赖

### GitHub Actions

- [actions/checkout at `3d3c42e5aac5`](https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1),
  released as `v7.0.1` when adopted.
- [actions/setup-python at `5fda3b95a4ea`](https://github.com/actions/setup-python/tree/5fda3b95a4ea91299a34e894583c3862153e4b97),
  released as `v7.0.0` when adopted.
- Use: immutable CI checkout and Python runtime setup.
- License: both projects use MIT licenses; the Actions are remotely executed
  and their source is not included in this repository.

## Personal-agent and Harness references / 个人 Agent 与 Harness 参考

### Hermes Agent

- Source: [NousResearch/hermes-agent at `d71033a4077a`](https://github.com/NousResearch/hermes-agent/tree/d71033a4077a6dfdcdb42c9e9eeab4c41e4a7012).
- Use: personal-assistant product framing, optional MCP tools, reusable Skills,
  memory, and multi-surface interaction patterns.
- License: [MIT](https://github.com/NousResearch/hermes-agent/blob/d71033a4077a6dfdcdb42c9e9eeab4c41e4a7012/LICENSE).
- Status: design reference only; no Hermes code, prompt, or documentation text
  is included.

### OpenClaw

- Source: [openclaw/openclaw at `521f45592e45`](https://github.com/openclaw/openclaw/tree/521f45592e457856703f5a9e9e1368f4aad52695)
  and its [VISION.md](https://github.com/openclaw/openclaw/blob/521f45592e457856703f5a9e9e1368f4aad52695/VISION.md).
- Use: local personal-assistant framing, extension boundaries, and the idea of
  learning from a personal playground while keeping product claims pragmatic.
- License: [MIT](https://github.com/openclaw/openclaw/blob/521f45592e457856703f5a9e9e1368f4aad52695/LICENSE).
- Status: design reference only; no OpenClaw code, prompt, or documentation text
  is included.

### OpenGUI and DeerFlow

- Sources: [Core-Mate/OpenGUI at `7cf28b908664`](https://github.com/Core-Mate/OpenGUI/tree/7cf28b90866459e74300869766896f953761dd60)
  and [bytedance/deer-flow at `1a1c5def0da3`](https://github.com/bytedance/deer-flow/tree/1a1c5def0da35e8347009fe1fed8e0e2321b0ede).
- Use: repository-specific architecture facts, layered guidance, and
  deterministic boundary-check patterns.
- License status: the audited OpenGUI revision uses Business Source License
  1.1; the audited DeerFlow revision uses MIT.
- Status: design references only; no source file or asset is included.

## Engineering-practice references / 工程实践参考

- [Google Engineering Practices: Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)
  for focused, reviewable changes (CC-BY-3.0 documentation).
- [Microsoft Engineering Fundamentals Playbook: Pull Requests](https://microsoft.github.io/code-with-engineering-playbook/code-reviews/pull-requests/)
  for risk-based review and evidence practices (CC-BY-4.0 documentation).
- [Tencent Technology Engineering article](https://mp.weixin.qq.com/s/mGGIbFyF4U1PrBJVdfgcvg)
  for staged narrowing, checkpoints, and evidence chains; reference-only, with
  no public reuse license relied upon.
- [obra/superpowers at `d884ae04edeb`](https://github.com/obra/superpowers/tree/d884ae04edebef577e82ff7c4e143debd0bbec99),
  [addyosmani/agent-skills at `2fbfa004a019`](https://github.com/addyosmani/agent-skills/tree/2fbfa004a0192529bc997d103fc12f19a3804aab),
  and [multica-ai/andrej-karpathy-skills at `2c606141936f`](https://github.com/multica-ai/andrej-karpathy-skills/tree/2c606141936f1eeef17fa3043a72095b4765b9c2)
  for reusable workflow, checkpoint, simplicity, and surgical-change concepts.
- Status: concepts only; no source file or substantial passage is included.

## Maintenance rule / 维护规则

发布复制或修改第三方代码、文字、图片、Prompt、超出兼容性事实的 Schema 或其他受保护
材料之前，必须记录精确来源和文件、核验许可范围、保留所需版权与 NOTICE、确认与本仓库
许可证兼容；无法确认时必须移除或独立重写。根 `LICENSE` 的版权主体或项目许可证如需
调整，必须由维护者另行明确决定，不能从普通实现授权中推断。

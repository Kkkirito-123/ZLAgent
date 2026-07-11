# Third-Party Notices

This file records third-party source, design references, and license boundaries
for the public ZLAgent repository.

## Repository License Layers

- ZLAgent root project code is licensed under the MIT License. See
  [LICENSE](./LICENSE).
- `OpenGUI-main/` is not relicensed by ZLAgent. It remains licensed under
  the Business Source License 1.1 (BUSL-1.1). See
  [OpenGUI-main/LICENSE](./OpenGUI-main/LICENSE).
- OpenGUI BUSL parameters in this repository:
  - Licensor: Core-Mate
  - Licensed Work: OpenGUI
  - Additional Use Grant: None
  - Change Date: 2030-04-29
  - Change License: Apache License, Version 2.0
- Before the Change Date, OpenGUI permits copying, modification,
  redistribution, and non-production use under BUSL-1.1. Production use,
  commercial use, hosted services, and integration into commercial products
  require a separate commercial license from Core-Mate.

The MIT license for ZLAgent does not override the BUSL-1.1 terms that apply to
`OpenGUI-main/`.

## Bundled OpenGUI Components

`OpenGUI-main/` is included as a source-available component and keeps its own
license boundary.

- `OpenGUI-main/server/`: OpenGUI backend, remote-control API, standby socket,
  execution socket, Plan Supervisor / Executor Graph, OpenGUI task/execution
  state, and IM channel modules.
- `OpenGUI-main/client/`: Android client modules including `app`,
  `feature_promotor`, `automation`, `core_accessibility`, `core_network`,
  `core_common`, `core_aop`, and `core_common_jvm`.
- `backend/tools/builtins/open_gui.py`: ZLAgent's MIT-licensed bridge to the
  OpenGUI backend. This bridge does not relicense OpenGUI itself.
- `backend/opengui/client.py`: ZLAgent's MIT-licensed OpenGUI REST client.
- `start.sh`, `status.sh`, `phone-wifi.sh`, and
  `scripts/opengui-supervisor.sh`: ZLAgent helper scripts for starting,
  checking, and connecting to OpenGUI. They rely on Android SDK / ADB tools
  when controlling a real phone.

## Android and Runtime Dependencies

OpenGUI Android Client uses Android SDK and ecosystem dependencies declared in
`OpenGUI-main/client/gradle/libs.versions.toml`, including Android Gradle
Plugin, Kotlin, KSP, AndroidX, Compose, Material, OkHttp, Retrofit, Gson,
socket.io-client, MMKV, JUnit, AndroidX Test, and Espresso.

These third-party packages are not relicensed by ZLAgent or OpenGUI; they
remain under their respective upstream licenses. Generated Gradle, Maven,
Android SDK, and ADB artifacts should be distributed only under the terms of
their upstream providers.

## MCP and Optional External Tooling

ZLAgent uses MCP (Model Context Protocol) as an external tool integration
surface. MCP server examples and the currently enabled `open-websearch` entry
are declared in `config/mcp_servers.yaml`.

- Python MCP SDK dependency: `mcp` in `requirements.txt`.
- Currently enabled MCP entry: `open-websearch@latest`, started through `npx`.
- Optional MCP examples in config include filesystem, Playwright, GitHub,
  YouTube transcript / yt-dlp, Lark / Feishu, MarkItDown, sequential thinking,
  fetch, portable memory, and time tools.

MCP servers launched through `npx`, `uvx`, or other package managers are
downloaded from their own upstream projects at runtime. Their licenses are not
covered by the ZLAgent MIT license; enable and distribute them only under the
terms of their respective upstream licenses.

## Main Python / Node Dependencies

ZLAgent Python dependencies are listed in `requirements.txt`, including
FastAPI, Uvicorn, SQLAlchemy, Pydantic, Loguru, httpx, croniter, PyYAML,
aiohttp, cryptography, qrcode, redis, fakeredis, and yt-dlp.

OpenGUI server dependencies are listed in `OpenGUI-main/server/package.json`,
including Node.js 22+, pnpm, TypeScript, Turbo, Biome,
`@larksuiteoapi/node-sdk`, `grammy`, and `streamdown`.

Each dependency remains under its upstream license. Package-manager lockfiles,
generated dependency directories, SDK caches, APKs, logs, and local runtime
artifacts should not be committed unless the project explicitly decides to
vendor them and records the corresponding license text here.

## Vendored or Ported Source

### Hermes Agent

- Upstream: https://github.com/NousResearch/hermes-agent
- License: MIT
- Copyright notice: Copyright (c) 2025 Nous Research
- Local use:
  - `backend/gateways/_vendor/weixin_ilink.py` is a focused port of
    Hermes Agent's `gateway/platforms/weixin.py`.
  - The local file preserves the MIT license text in its header.
  - Several ZLAgent subsystems also reference Hermes design patterns, including
    memory, tool-call guardrails, context compression, skill review, and cron
    workflow ideas.

### OpenClaw

- Upstream: https://github.com/openclaw/openclaw
- License: MIT
- Copyright notice: Copyright (c) 2026 OpenClaw Foundation
- Local use:
  - `backend/mcp/lifecycle.py` copies the blocked MCP subprocess environment
    variable policy from OpenClaw's `host-env-security-policy.json`.
  - ZLAgent also references OpenClaw's safety approach for tool permission
    levels, dangerous action confirmation, and MCP installation hardening.

## Design References Without Vendored Source

### learn-claude-code

- Upstream: https://github.com/shareAI-lab/learn-claude-code
- License: MIT
- Copyright notice: Copyright (c) 2024 shareAI Lab
- Local use:
  - Architecture and workflow reference for building an agent from main loop,
    tools, permissions, skills, memory, background tasks, scheduled tasks, and
    MCP extension points.
  - ZLAgent reimplements these ideas for an IM-first personal assistant.

### nvk/llm-wiki

- Upstream: https://github.com/nvk/llm-wiki
- License: MIT
- Copyright notice: Copyright (c) 2026 nvk
- Local use:
  - Design reference for reusable answer caching, confidence, source tracking,
    concept pages, and wiki-style crystallization.
  - ZLAgent implements its own Python wiki/cache layer in `backend/wiki/` and
    related skill flows.

### andrej-karpathy-skills

- Upstream: https://github.com/forrestchang/andrej-karpathy-skills
- Upstream metadata: README / plugin metadata marks the project as MIT.
- Local use:
  - Prompting guideline reference for Think Before Coding, Simplicity First,
    Surgical Changes, and Goal-Driven Execution.
  - ZLAgent uses translated/adapted principles in system prompts and skill
    review prompts; no upstream source code is vendored.

## Compliance Rules for Future Changes

- Keep original copyright and license notices with any copied or substantially
  ported upstream source.
- Add any new copied source, policy table, prompt pack, asset, or template to
  this notice file before public release.
- Do not remove or weaken `OpenGUI-main/LICENSE`.
- Do not describe the combined repository as pure MIT while `OpenGUI-main/` is
  included.
- If OpenGUI is used in production, commercial products, hosted services, or
  other non-BUSL-permitted contexts before the Change Date, obtain a commercial
  license from Core-Mate or remove/replace that component.

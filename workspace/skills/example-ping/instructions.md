# example-ping

This is the seed skill bundled with ZLAgent. It exercises the legacy
`skill.yaml` + `instructions.md` loader path and gives humans a known-good
reference for the on-disk layout.

## Overview

When the agent sees `example-ping` in a skill hint, it should respond with a
short "pong" acknowledgement and no side effects. The skill intentionally
has no tool calls — it is a liveness check, nothing more.

## Steps

1. Read the user's latest message.
2. Reply with a short `pong` text that mirrors the user's tone.
3. Do not call tools. Do not write files.

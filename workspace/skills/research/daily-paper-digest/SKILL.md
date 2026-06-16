---
name: daily-paper-digest
description: |
  Scheduled or on-demand digest of recent papers in a chosen field, with
  source URLs and a one-line takeaway per paper. Used as the skill_hint
  for cron jobs like "每天 8 点给我发最新论文".
version: 0.1.0
tags:
  - research
  - digest
  - scheduled
metadata:
  zlagent:
    category: research
    triggers:
      - 最新论文
      - 每天论文
      - 每周论文
      - paper digest
      - daily paper
      - 论文推送
      - arxiv 推送
    capabilities:
      - web_query
      - cron
    related_skills:
      - arxiv
      - web-summary
      - news-digest
---

# daily-paper-digest

## Trigger

Use this skill when the user asks for a one-time or scheduled digest of
recent papers in some topic (e.g. "每天 8 点给我发 AI Agent 最新论文",
"每周一总结机器学习论文"). When the schedule fires through cron, this
skill is loaded automatically via `skill_hint`.

Do NOT use this skill for one-off "查一下某篇论文" questions — that path
goes through `web_search` / `read_url` directly without scheduling.

## Inputs

- `topic`: subject area or keywords (e.g. "AI Agent / LLM"). When the
  cron instruction or chat does not name a topic, fall back to the most
  recent topic the user mentioned in memory; if still empty, ask once
  instead of inventing a topic.
- `window`: time window — default "last 24h" for daily, "last 7d" for
  weekly digests.
- `max_results`: default 5, hard cap 8.
- `sources`: arXiv first, then `web_search` for venue / press. Prefer
  arxiv.org and well-known preprint mirrors.

## Steps

1. Resolve `topic`. If missing and not recoverable, send a single short
   ask back instead of running a search.
2. Pull candidate papers. Reuse the `arxiv` skill as the primary source;
   fall back to `web_search` + `read_url` for non-arxiv venues.
3. Rank by relevance to `topic`, then recency. Drop duplicates by arxiv
   id or canonical URL.
4. For each paper produce:
   - title
   - primary authors (max 3, then `et al.`)
   - year
   - source URL
   - one-line plain-language takeaway
   - optional "为什么值得看" line when the paper is genuinely notable
5. Output as a compact IM message — short bullets, newest first, no
   markdown headings per paper.

## Verification

- Every paper has a real source URL.
- Title, authors, year are never invented.
- If the result set is empty or thin, say so plainly with the count
  rather than padding with low-confidence hits.
- For scheduled runs, only emit `[SILENT]` when the topic itself is
  unresolved and the user has not yet provided one.

## Failure Signals

- `topic` missing and not recoverable from memory or chat.
- All candidate sources return errors.
- Same paper repeats across consecutive runs (dedupe is failing).
- Output reads like an abstract dump rather than a takeaway summary.

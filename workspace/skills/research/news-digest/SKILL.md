---
name: news-digest
description: |
  Compact news roundup on a topic: web_search for fresh items, dedup,
  read 1-2 sources per item, return short bullets with links.
  Use as the skill_hint for "每天 8 点给我发新闻" cron jobs.
version: 0.1.0
tags:
  - research
  - news
  - scheduled
metadata:
  zlagent:
    category: research
    triggers:
      - 今天新闻
      - 最新新闻
      - 新闻摘要
      - news digest
      - 每日新闻
    capabilities:
      - web_query
      - cron
    related_skills:
      - web-summary
      - daily-paper-digest
---

# news-digest

## Trigger

Use this skill when the user asks for a one-off or scheduled digest of
news in a topic (tech, finance, sports, a specific company, ...). When
a cron job fires with `skill_hint=news-digest`, this skill is loaded
automatically.

Do NOT use this for:
- Single-article summaries → `web-summary`.
- Academic papers → `daily-paper-digest`.
- Verifying a specific claim → direct `web_search` then `read_url`.

## Inputs

- `topic`: subject area (e.g. "AI 创业", "OpenAI", "电动车"). If
  missing, ask once instead of inventing one.
- `window`: time window — default "last 24h" for daily, "last 7d" for
  weekly.
- `max_items`: default 5, hard cap 8.
- `region`: optional geographic / language hint (e.g. "中文媒体").

## Steps

1. `web_search` for `<topic>` filtered to the window. Prefer reputable
   outlets, drop obvious aggregators / SEO farms.
2. Dedup by canonical URL, then by headline-similarity > 0.8.
3. For the top items, `read_url` only when the headline alone isn't
   enough (e.g. ambiguous, possible clickbait). Skip when it is.
4. For each item produce: headline, source domain, one-line takeaway.
5. Order newest first. Keep the whole reply IM-sized — no per-item
   markdown headings, no quote dumps.

## Verification

- Every item carries a real URL.
- Headlines and dates are not invented.
- If fewer than `max_items` quality items exist, return the smaller
  count and say so — do not pad.
- For scheduled runs, only return `[SILENT]` when the topic itself is
  unresolved.

## Failure Signals

- `topic` missing and not recoverable.
- All search results blocked / paywalled.
- Same story repeats across the digest (dedup is failing).
- Output reads like raw search results rather than a curated digest.

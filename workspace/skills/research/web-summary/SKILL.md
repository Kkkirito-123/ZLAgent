---
name: web-summary
description: |
  Summarise a single web URL the user shares: fetch with read_url,
  produce a compact key-points reply. No external CLI, no API key.
version: 0.1.0
tags:
  - research
  - summary
metadata:
  zlagent:
    category: research
    triggers:
      - 总结一下
      - 看一下这个链接
      - 这篇讲了什么
      - summarize this
      - tldr
      - what is this article about
    capabilities:
      - web_query
    related_skills:
      - news-digest
      - daily-paper-digest
      - arxiv
---

# web-summary

## Trigger

Use this skill when the user shares a single URL (article, blog post,
documentation page, README, news piece) and asks what it says or wants
a short summary. Examples:

- "https://example.com 看一下讲什么"
- "tldr https://example.com/post"
- "总结这个 PR 的描述"

Do NOT use this when the user wants:
- A multi-source roundup → use `news-digest`.
- Academic papers → use `arxiv` / `daily-paper-digest`.
- Just to download / extract the page content as-is.

## Inputs

- `url`: the URL to summarise. If multiple URLs, pick the first and ask
  whether to also process the others.
- `focus`: optional keyword the summary should bias toward (e.g. "只讲
  和定时任务相关的部分").
- `length`: default "short" (~150 chars body) for IM, "medium" (~400)
  when the user asks for more detail.

## Steps

1. Validate the URL (must be http / https; refuse data: / file:).
2. Call `read_url(url)`. If the response is gated (login wall, 403),
   say so plainly and stop — never guess content.
3. Strip nav / footer / ad text; focus on the article body.
4. Produce 3 sections in the reply:
   - 1-line elevator pitch
   - 3-5 bullet key points (newest / most actionable first when ordered)
   - 1-line takeaway or "为什么值得看"
5. Keep within `length` budget. Cite the URL once at the end.

## Verification

- Every claim must come from the fetched body — never invent details.
- If body was truncated, say so and offer to re-fetch with a different
  range / section anchor.
- For tech docs, distinguish "the doc says X" from "X is generally true".

## Failure Signals

- `read_url` returned a paywall, error, or empty body.
- Output reads like the original article rather than a summary.
- Same paragraph paraphrased multiple times (no real compression).
- Numbers / dates / quotes don't match the source.

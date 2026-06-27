---
name: arxiv
description: |
  Fetch and summarise recent papers from arxiv.org. Use this skill when the
  user wants a digest of today's or this week's cs.AI / cs.LG / cs.CL
  listings, a focused per-topic roundup, or a quick look at a specific
  submission.
tags:
  - research
  - digest
version: 0.1.0
---

# arxiv

## Overview

Scan arxiv for new papers matching the user's request and produce a
compact digest. Favour titles, authors, and a one-sentence takeaway per
paper over copy-pasting abstracts wholesale.

## Steps

1. Decide the scope: a category (e.g. `cs.AI`), a specific arxiv id, or a
   keyword query. Default to `cs.AI` for the last 24 hours when the user
   is vague.
2. Fetch the list via `read_url` against the appropriate
   `https://arxiv.org/list/<category>/new` or
   `https://arxiv.org/abs/<id>` URL. For keyword queries prefer
   `https://export.arxiv.org/api/query?search_query=...`.
3. Parse the titles / authors / abstracts. Skip cross-listings the user
   already implicitly excluded.
4. Produce 3–5 short bullets per paper: title, primary authors, one-line
   plain-language takeaway. No markdown headings per paper — the chat
   surface is a messenger, not a document.
5. When nothing useful shipped in the requested window, say so plainly
   rather than padding the reply.

## Output format

- Line 1: one-line summary of the scope and count (e.g. "cs.AI, 3 new
  papers in the last 24 hours").
- One bullet per paper, newest first.
- End with a short "note:" line only when there is a non-obvious caveat
  (rate-limit hit, partial results, timezone mismatch).

## Pitfalls

- The arxiv HTML listing is *long*; ask `read_url` to truncate at the
  listing section rather than loading every byte.
- The arxiv API occasionally returns 503s — retry once, then report
  plainly instead of inventing content.
- Do not speculate on experimental claims the abstract does not make.

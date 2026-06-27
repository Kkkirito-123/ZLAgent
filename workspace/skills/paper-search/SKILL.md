---
name: paper-search
description: |
  Find a small list of academic papers on demand — "给我 N 篇目标检测领域
  顶刊 / 顶会 / SOTA 论文 / 综述" — using the multi-source chain
  arxiv API → Semantic Scholar → web_search. Distinct from
  `daily-paper-digest` (scheduled daily delivery) and `arxiv` (single-day
  category dump): this one is the on-demand "find me N papers about X"
  surface that the user usually triggers in chat.
version: 0.1.0
tags:
  - research
  - papers
  - on-demand
metadata:
  zlagent:
    category: research
    triggers:
      - 顶刊
      - 顶会
      - 几篇论文
      - 几个论文
      - 篇论文
      - 找.*论文
      - 推荐.*论文
      - SOTA
      - state-of-the-art
      - 最新进展
      - 综述
      - survey
      - 对比.*论文
      - benchmark
    capabilities:
      - web_query
      - read_url
    related_skills:
      - arxiv
      - daily-paper-digest
      - web-summary
---

# paper-search

## Trigger

Use this skill when the user asks for a small ad-hoc list of academic
papers, typically:

- "给我目标检测领域的 5 篇顶刊论文，要最新的"
- "AI agent 方向最近半年的 SOTA 论文有哪些"
- "推荐 3 篇关于 RAG 评估的综述"
- "Find me 5 recent papers on diffusion video generation"

Do NOT use this skill for:

- Scheduled daily / weekly digests → `daily-paper-digest`.
- "Today's cs.AI listing on arxiv" (category dump, not topic-targeted)
  → `arxiv`.
- Reading / summarising a single paper the user already linked →
  `web-summary` + `read_url`.

## Inputs

Resolve from the user message before fetching:

- `topic` — the research area in plain words ("目标检测 / object
  detection", "RAG evaluation"). Required; ask one short follow-up if
  truly ambiguous.
- `count` — how many papers, default 5, hard cap 8.
- `recency` — "最新 / latest" → last 12 months; "近年 / recent" → last
  3 years; explicit window like "2024 以来" → respect verbatim.
- `kind` — empirical / SOTA / survey / benchmark / methods. Default:
  whichever the user phrased; if silent, prefer SOTA + 1 survey when
  both are obviously useful.

## Steps

1. **Source priority** (cheapest + highest signal first):
   - **arxiv API** — `https://export.arxiv.org/api/query?search_query=ti:<topic>+OR+abs:<topic>&start=0&max_results=20&sortBy=submittedDate&sortOrder=descending`
     via `read_url`. Returns Atom XML; parse for title / authors /
     summary / arxiv id. Best for ML / CS / EE.
   - **Semantic Scholar Graph API** — `https://api.semanticscholar.org/graph/v1/paper/search?query=<topic>&limit=20&fields=title,authors,year,venue,citationCount,abstract,url`
     via `read_url`. Better venue + citation metadata than arxiv. If
     it returns 429, wait and retry **once** with a 60s gap; then move
     on without it.
   - **web_search** — only as a last fallback when arxiv + S2 both
     yielded fewer than `count` candidates. Query template:
     `"<topic>" survey 2024 site:arxiv.org` then `site:openreview.net`.

2. **Filter + rank**:
   - Drop preprints whose abstract is empty / "Withdrawn".
   - Prefer top-tier venues when the user asked for 顶刊 / 顶会 (CV:
     CVPR / ICCV / ECCV / TPAMI; ML: NeurIPS / ICML / ICLR; NLP: ACL
     / EMNLP / NAACL; speech: INTERSPEECH; systems: SOSP / OSDI;
     security: S&P / USENIX). Surface arxiv-only entries only when
     no venued match exists.
   - Sort: recency desc → citation desc → venue tier.
   - Dedupe by arxiv id / DOI / canonical URL.

3. **Render** (one bullet per paper, newest first):
   - Title (in original language).
   - Primary authors (max 3 then `et al.`).
   - Year + venue if known.
   - One-line plain-language takeaway (≤ 80 chars).
   - Source URL (arxiv abs preferred over PDF).

4. **Tool budget**: aim for ≤ 3 tool calls total. List-task
   adaptive budget already grants up to 10 — reserve the headroom
   for retries, not for browsing every candidate.

## Verification

- Every paper has a real source URL.
- Title / authors / year are never invented; if unknown, omit the
  field and say so plainly.
- If the result set is thin (< `count` solid hits), say "找到 N 篇
  够稳的，剩下的来源都不太可信，先给你这 N 条" — do not pad.
- Do NOT ask the user to "确认是否保存到知识库" unless they signalled
  long-term storage intent. Default is in-chat output only.

## Pitfalls

- Semantic Scholar 429 is common; honour the `Retry-After` header
  and degrade silently to arxiv-only.
- DDG / Bing captcha will trigger the v0.45 provider chain in
  `web_search`; if all 3 web providers fail, fall back to whatever
  arxiv + S2 produced even if `< count`.
- "顶刊" sometimes means **journal** (TPAMI / TMLR) and "顶会" means
  **conference** (CVPR / NeurIPS); when the user uses both,
  include at least one of each.
- Don't translate paper titles — keep the originals so the user can
  search them as-is.

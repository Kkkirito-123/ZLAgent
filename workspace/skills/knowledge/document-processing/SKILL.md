---
name: document-processing
description: |
  Umbrella skill for user document workflows — PDF, Word, Excel,
  PowerPoint, text extraction, summarization. Dispatches to the right
  MCP / tool; proposes an install via mcp-discovery when no local
  capability fits. Adapted from Anthropic's docx/pdf/xlsx/pptx skills.
version: 0.1.0
tags:
  - knowledge
  - document
  - workflow
metadata:
  hermes:
    created_by: user
  zlagent:
    category: knowledge
    triggers:
      - 帮我读 PDF
      - 解析一下这个文件
      - extract from pdf
      - read docx
      - 从 word 里取
      - excel 里
      - ppt 里
      - 转成 pdf
    capabilities:
      - read_url
      - read_file
      - mcp_discovery
    related_skills:
      - mcp-discovery
      - web-summary
      - documentation-and-decisions
---

# document-processing

## Trigger

User sends a document (PDF / Word / Excel / PowerPoint / plain text)
or asks to extract, summarise, or edit one. Also triggers when the
user references a file path or URL ending in `.pdf`, `.docx`,
`.xlsx`, `.pptx`, `.txt`, `.md`.

If the user mentions several files or links at once, treat this as a
multi-document request and either process them in sequence or ask
which one should go first.

## Principle

> Route each document kind to the right specialised tool. Never invent
> content that is not in the document.

## Decision: which backend?

1. **Plain text / markdown** (`.txt`, `.md`, `.log`):
   → use `read_file` (if local) or `read_url` (if remote).
2. **Web-published HTML / article**:
   → `web-summary` skill.
3. **PDF**:
   → if a PDF-aware MCP is installed (look for `mcp__pdf__*` or a
     plugin tool exposing pdf_read), use it. Otherwise propose
     installing an MCP via `mcp-discovery` (candidate examples:
     `mcp-server-pdf`, `pdfplumber-mcp`). Never fabricate content by
     guessing.
4. **Word (`.docx`)**:
   → similar: `mcp__docx__*` or propose discovery.
5. **Excel (`.xlsx`)**:
   → `mcp__xlsx__*` or `code_execution` with openpyxl for small local
     files, else propose MCP.
6. **PowerPoint (`.pptx`)**:
   → `mcp__pptx__*` or propose MCP.

## Inputs

- `source`: file path (inside workspace) or URL.
- `format`: explicit override when the extension lies (common for
  URLs without suffix).
- `operation`: `"extract"` | `"summarise"` | `"search"` |
  `"convert"`.
- `fields` (optional): specific fields or pages to target.

## Steps

1. Identify the document kind. If ambiguous, ask the user once.
2. Check the harness inventory for a matching MCP / plugin via
   `tool_search` with the file type as query.
3. If no backend exists and the user wants to proceed:
   - Route to `mcp-discovery`.
   - Propose ONE candidate MCP with a one-line reason.
   - Wait for confirmation before install.
4. If the user provided multiple documents, process them in the order given or ask which one to prioritise.
5. On success, extract / summarise / convert as requested.
6. Cite the page / sheet / slide numbers in the answer.

## Verification

- Every fact in the reply can be pointed to a specific page / sheet /
  cell.
- If the extraction returned partial results (rate limit, bad PDF,
  password-protected), say so plainly — do not fill gaps with
  speculation.
- For conversions, the output file is actually created and its size
  is non-zero.

## Failure Signals

- The reply contains information that isn't in the source — stop,
  re-read the actual document.
- "I cannot read PDFs" response with no mcp-discovery attempt — the
  skill's job is to propose a path, not refuse.
- Installing multiple MCP servers for the same file kind — consolidate.

## Output Format

```
文档：<source>  (<kind>, <size/pages>)
操作：<operation>

<内容摘要或提取，带页码 / 表格 / 列引用>

来源：p.X-Y / sheet:<name> / slide:<N>
```

If proposing an install:

```
这个文档需要 <kind> 支持，本地没有合适的工具。
建议装：<mcp name>  —  <一句话理由>
装吗？（yes/no）
```

---
name: weather-now
description: |
  Look up current weather, short forecast, or rain check for a city
  using wttr.in. No API key, no new tool — uses read_url against
  https://wttr.in/<city>?format=...
version: 0.1.0
tags:
  - utility
  - weather
metadata:
  zlagent:
    category: research
    triggers:
      - 天气
      - 下雨
      - 气温
      - 多少度
      - weather
      - forecast
      - rain check
    capabilities:
      - web_query
    related_skills: []
---

# weather-now

## Trigger

Use this skill when the user asks for current weather, short-range
forecast, rain probability, or temperature for a named place. Examples:

- "今天上海下雨吗"
- "北京气温现在多少"
- "weekend weather in Tokyo"

Do NOT use this for historical climate, severe-weather alerts, marine /
aviation weather, or hyper-local microclimate questions — those need
specialised sources.

## Inputs

- `location`: city, region or airport code. If the user did not name a
  place, ask once. Do not invent a default.
- `mode`: one of `now` (default), `today`, `forecast` (3-day),
  `rain` (precipitation focus).

## Steps

1. URL-encode the location (replace spaces with `+`).
2. Call `read_url` with one of:
   - now / rain: `https://wttr.in/<loc>?format=3`
   - today: `https://wttr.in/<loc>?0`
   - forecast: `https://wttr.in/<loc>?format=v2`
3. Trim the response to the relevant lines. wttr.in returns ASCII art
   for full forecasts — keep only the summary line + per-day temps.
4. Reply in one short message. Use Celsius and the user's language
   (Chinese or English) inferred from the question.
5. End with the location and the time-of-check (Beijing time) so
   the user can tell when the data was fetched.

## Verification

- Output must contain the resolved location string.
- Numeric temperature / precipitation values come from the response, not
  invented.
- For rain checks, state probability or "无降水"; do not guess.
- If `read_url` returns 5xx or empty, say so plainly and offer to retry.

## Failure Signals

- Location ambiguous (e.g. "我家附近") and not recoverable from memory.
- wttr.in unreachable or returns rate-limit message.
- Reply contains ASCII art / tables that don't render in IM.
- User asked for severe-weather alerts or aviation weather — refuse
  and recommend an official source.

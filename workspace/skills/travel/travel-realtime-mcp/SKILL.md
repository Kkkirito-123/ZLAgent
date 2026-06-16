---
name: travel-realtime-mcp
description: >
  Handle time-sensitive travel requests by using built-in realtime travel
  lookup or attaching MCP servers for railway tickets, flights, maps, local
  places, weather, hotels, and route planning. Prefer a quick answer path:
  call the built-in travel_realtime tool first for 12306/Open-Meteo; use
  existing mcp__* tools next; otherwise propose one safe candidate and install
  only after confirmation.
version: 0.2.0
tags:
  - travel
  - mcp
  - realtime
metadata:
  hermes:
    created_by: user
  zlagent:
    category: mcp
    triggers:
      - 实时票价
      - 高铁余票
      - 余票
      - 火车票
      - 火车
      - 高铁
      - 动车
      - 车次
      - 票价
      - 12306
      - 机票
      - 航班
      - 酒店价格
      - 今晚酒店
      - 实时天气
      - 天气
      - 气温
      - 温度
      - 下雨
      - 降雨
      - 风速
      - 今天限行
      - 地图
      - 导航
      - 附近
      - 当前位置
      - google maps
      - flight price
      - hotel price
      - live weather
      - route planning
      - travel mcp
    capabilities:
      - mcp_lifecycle
      - realtime_lookup
    related_skills:
      - travel-guide
      - mcp-discovery
---

# travel-realtime-mcp

## Overview

Use this skill when the user wants **fresh travel data** rather than a generic itinerary: current train tickets, flight prices, hotel availability, local weather, map routes, nearby places, or airport/route status.

This skill is the tool-driven complement to `travel-guide`: `travel-guide` is optimized for fast streaming, knowledge-based chunks; this skill is for tool/MCP-backed live lookups and should not invent realtime numbers.

## When to Use

Use this skill for:

- Chinese railway / 12306 ticket availability, train times, or station-to-station options.
- Flight search, flight prices, transfer routes, flight number status, airport weather.
- Google Maps / Places / route planning / nearby food / route duration / timezone / air quality.
- Hotel price or availability lookups.
- User explicitly asks to install, find, or use a travel MCP.

Do not use this skill for a generic "三日游攻略" / "去哪玩" request with no realtime requirement. Let `travel-guide` answer those quickly with streaming sections.

## Candidate MCP Servers

### 1. 12306-mcp

Best for China railway ticket search.

- Source: `Joooook/12306-mcp`
- Install shape:

```json
{
  "name": "rail_12306",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "12306-mcp"],
  "description": "China railway 12306 ticket search"
}
```

- API key: none shown in README.
- Default permission: keep tools `confirm` until inspected.

### 2. Google Maps MCP via `@cablate/mcp-google-map`

Best for places, nearby search, directions, distance matrix, route planning, weather, air quality, timezone, and local route reasoning.

- Source: `cablate/mcp-google-map`
- Install shape:

```json
{
  "name": "google_maps_travel",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@cablate/mcp-google-map", "--stdio"],
  "env": {
    "GOOGLE_MAPS_API_KEY": "<user-provided-key>",
    "GOOGLE_MAPS_ENABLED_TOOLS": "maps_geocode,maps_search_places,maps_place_details,maps_directions,maps_distance_matrix,maps_weather,maps_search_nearby,maps_plan_route"
  },
  "description": "Google Maps places routes weather"
}
```

- API key: requires `GOOGLE_MAPS_API_KEY`; Places API and Routes API should be enabled.
- Default permission: keep `confirm` unless the user explicitly authorizes read-only maps calls as `safe`.

### 3. Travel Planner MCP via `@gongrzhe/server-travelplanner-mcp`

Best for a smaller Google Maps travel-planning surface: places, place details, routes, timezone.

- Source: `GongRzhe/TRAVEL-PLANNER-MCP-Server`
- Install shape:

```json
{
  "name": "travel_planner_maps",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@gongrzhe/server-travelplanner-mcp"],
  "env": {
    "GOOGLE_MAPS_API_KEY": "<user-provided-key>"
  },
  "description": "Travel planner maps places routes"
}
```

- Tools advertised by README: `searchPlaces`, `getPlaceDetails`, `calculateRoute`, `getTimeZone`.
- Prefer this over the larger Google Maps MCP when the user only needs place search and routing.

### 4. Variflight MCP

Best for China/international flight data, flight number lookup, transfer search, realtime aircraft location, airport weather, and flight prices.

- Source: `variflight/variflight-mcp`
- Install shape:

```json
{
  "name": "variflight",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@variflight-ai/variflight-mcp"],
  "env": {
    "VARIFLIGHT_API_KEY": "<user-provided-key>"
  },
  "description": "Variflight flight search status prices"
}
```

- API key: requires `VARIFLIGHT_API_KEY`.
- Default permission: keep `confirm`; flight/location queries can reveal travel intent.

### 5. Flight/hotel search pack

Use only if the user explicitly wants flight + hotel search and can provide third-party API keys.

- Source: `RikGmee/searchAPI-mcp`
- Capabilities seen in README: `search-flights`, `search-hotels`, `today`.
- Requires Booking.com/RapidAPI key and often Google Maps API key.
- Treat as a second-stage option, not the default quick path.

### 6. Full travel assistant MCP ecosystem

Use only when the user wants a larger travel-agent stack with flights, hotels, events, weather, geocoding, currency, and budget orchestration.

- Source: `skarlekar/mcp_travelassistant`
- Capabilities: `search_flights`, `search_hotels`, `search_events`, `get_weather_forecast`, `geocode_location`, `calculate_distance`, `convert_currency`.
- Requires multiple local server directories plus keys such as SerpAPI. This is powerful but heavier than ZLAgent's quick-reply goal.

## Steps

1. Classify the request:
   - Generic plan: answer with `travel-guide` instead.
   - Realtime data: continue here.

2. For China rail / weather, call `travel_realtime` first:
   - `action="bundle"` when the user asks a mixed request such as "上海到北京 4 天，火车票和天气".
   - `action="rail_12306"` for train availability / 12306 / 高铁余票.
   - `action="weather_forecast"` for local weather / temperature / rain / wind parameters.

3. Check for existing matching MCP tools in the visible tool list:
   - `mcp__rail_12306__*` for trains.
   - `mcp__google_maps_travel__*` or `mcp__travel_planner_maps__*` for maps/routes/places/weather.
   - `mcp__variflight__*` for flights.
   - If present, call the existing tool and answer in a short IM format.

4. If no matching tool exists, do **not** fabricate realtime data. Propose at most two candidates:
   - One fastest/smallest candidate.
   - One fuller candidate if clearly useful.

5. If a key is required, ask for the env var name only; never ask the user to paste a secret into a public group chat. Tell them it will be passed as the MCP subprocess environment.

6. After the user chooses and provides/authorizes credentials, call `mcp_manage(action="add", ...)` using the JSON shape above. The tool itself will trigger IM confirmation.

7. After install, call `mcp_manage(action="inspect", name="...")` and verify tools are listed. If connection failed, remove the server and explain the failure.

8. Once connected, answer the original travel question using the newly available MCP tool.

## Quick Reply Formats

### No MCP installed yet

```text
这个问题需要实时数据，我不能编。
可接：
1. 12306-mcp：查高铁/火车余票，无需 API key。
2. Google Maps MCP：查路线/附近/天气，需要 GOOGLE_MAPS_API_KEY。
你选哪个？我会走确认安装。
```

### Tool lookup succeeded

```text
🚄 实时结果
- 最推荐：<车次/航班/路线> — <时间> — <价格/耗时>
- 备选：<...>

✅ 我的建议：<一句话>
```

### Tool lookup failed

```text
实时查询失败：<短原因>
我可以改用离线攻略先给路线骨架，或帮你换一个 MCP 数据源。
```

## Pitfalls

- Do not mix streaming `travel-guide` with MCP calls. Streaming sections are optimized for speed and may not call tools.
- Do not install every candidate. Install the smallest server that answers the current request.
- Do not mark tools `safe` by default. External APIs can cost money and reveal travel intent.
- Do not invent hotel prices, flight prices, train availability, weather, or live route times.
- Do not pass API keys unless the user explicitly supplied and authorized them.

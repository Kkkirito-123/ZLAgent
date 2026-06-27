from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ...tools.base import Tool, ToolPermission, ToolResult

SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# split timeouts. Prefetch happens on the hot IM path where the
# user is waiting, so we cap total work at ~12s. Individual calls use 6s.
DEFAULT_TIMEOUT = 12.0
PER_CALL_TIMEOUT = 6.0
STATION_NAME_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
RAIL_ENDPOINTS = ("queryG", "queryZ", "query")
WEATHER_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# short-TTL bundle cache so repeat realtime queries within ~60s
# don't hit 12306/Open-Meteo again. 12306 left-ticket counts shift every
# minute, so we keep TTL conservative.
_BUNDLE_CACHE: dict[str, tuple[float, "ToolResult"]] = {}
_BUNDLE_CACHE_TTL = 60.0
_BUNDLE_CACHE_MAX = 64

# optional Redis layer. Set via ``set_bundle_cache_redis_backend``
# from the app wiring. When configured we serve bundle results across
# workers (a colleague's "上海到北京 明天" warms the cache for everyone)
# while the per-process dict above stays as a zero-network L0 cache.
_BUNDLE_REDIS_BACKEND: Any = None
_BUNDLE_REDIS_TTL_SECONDS: int = int(_BUNDLE_CACHE_TTL)


def set_bundle_cache_redis_backend(
    backend: Any, *, ttl_seconds: Optional[int] = None,
) -> None:
    """Wire (or unwire) the Redis backend used by ``_bundle_cache_*``.

    ``app.py`` calls this once at startup with the shared ``RedisBackend``
    instance.  Smoke tests call it with ``None`` to detach.  We keep it
    a module-level singleton because :class:`TravelRealtimeTool` is
    instantiated by the registry at import time and we don't want to
    plumb the backend through every constructor.
    """
    global _BUNDLE_REDIS_BACKEND, _BUNDLE_REDIS_TTL_SECONDS
    _BUNDLE_REDIS_BACKEND = backend
    if ttl_seconds is not None and ttl_seconds > 0:
        _BUNDLE_REDIS_TTL_SECONDS = int(ttl_seconds)


def _bundle_redis_key(cache_key: str) -> str:
    backend = _BUNDLE_REDIS_BACKEND
    if backend is None:
        return ""
    # Fold the long native key into a short hex digest so we don't push
    # multi-hundred-byte keys through Redis. Collision-resistant for our
    # purposes (~64 active routes/day per worker fleet).
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:24]
    return backend.namespaced("travel", "bundle", digest)

_STATION_CODES: Optional[dict[str, str]] = None
# 12306 leftTicket APIs return an empty body unless we first
# hit ``/otn/leftTicket/init`` so the server seeds JSESSIONID +
# BIGipServerotn cookies. We track which client objects we've warmed so
# we don't pay the round-trip more than once per HTTP client lifetime.
_LEFT_TICKET_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"

_CITY_STATION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "北京": ("北京南", "北京", "北京西", "北京朝阳", "北京北"),
    "上海": ("上海虹桥", "上海", "上海南", "上海西"),
    "广州": ("广州南", "广州", "广州东", "广州白云"),
    "深圳": ("深圳北", "深圳", "福田", "深圳坪山"),
    "杭州": ("杭州东", "杭州西", "杭州"),
    "南京": ("南京南", "南京"),
    "天津": ("天津西", "天津", "天津南"),
    "重庆": ("重庆北", "重庆西", "沙坪坝", "重庆"),
    "成都": ("成都东", "成都西", "成都南", "成都"),
    "武汉": ("武汉", "汉口", "武昌"),
    "西安": ("西安北", "西安"),
    "苏州": ("苏州北", "苏州", "苏州园区"),
}

_WEATHER_CODE_CN: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


class TravelRealtimeTool(Tool):
    name = "travel_realtime"
    description = (
        "Query real-time travel data from public sources. Use this before answering"
        " travel requests that mention actual train tickets, 12306 availability,"
        " ticket prices, departure dates, local weather, temperature, rain, wind,"
        " or other live parameters. Supports action='rail_12306' for China railway"
        " availability, action='weather_forecast' for Open-Meteo weather, and"
        " action='bundle' to infer route/date/days from a Chinese travel query and"
        " return both train and destination weather sections. Do not estimate live"
        " train/weather values when this tool can be used."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 12_000
    search_hint = "travel realtime 12306 train rail ticket weather open meteo 火车票 高铁 余票 天气 气温 降雨"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["bundle", "rail_12306", "weather_forecast"],
                "description": "Which real-time lookup to perform.",
            },
            "query": {
                "type": "string",
                "description": "Original Chinese/English user query. Used by action='bundle' to infer cities, date, and days.",
            },
            "from_city": {"type": "string", "description": "Rail origin city or station, e.g. 上海 or 上海虹桥."},
            "to_city": {"type": "string", "description": "Rail destination city or station, e.g. 北京 or 北京南."},
            "date": {"type": "string", "description": "Departure date as YYYY-MM-DD, or Chinese relative date such as 今天/明天/后天."},
            "destination": {"type": "string", "description": "Weather destination city, defaults to to_city for bundle."},
            "days": {"type": "integer", "description": "Forecast days, 1-16. Defaults to trip days inferred from query or 4."},
            "limit": {"type": "integer", "description": "Maximum train rows to return, 1-20. Defaults to 8."},
            "only_available": {"type": "boolean", "description": "If true, hide trains where all common seats are unavailable."},
        },
        "required": ["action"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "bundle").strip()
        if action == "weather_forecast":
            return await self._weather_forecast(arguments)
        if action == "rail_12306":
            return await self._rail_12306(arguments)
        if action == "bundle":
            inferred = _infer_from_query(str(arguments.get("query") or ""))
            merged = {**inferred, **{k: v for k, v in arguments.items() if v not in (None, "")}}
            return await self._bundle(merged)
        return ToolResult(ok=False, content="", error=f"unknown action: {action}")

    async def _bundle(self, arguments: dict[str, Any]) -> ToolResult:
        from_city = str(arguments.get("from_city") or "").strip()
        to_city = str(arguments.get("to_city") or "").strip()
        departure_date = _normalize_date(str(arguments.get("date") or ""))
        destination = str(arguments.get("destination") or to_city or "").strip()

        # short-TTL cache hit avoids hammering 12306/Open-Meteo
        # when the user retries the same query within a minute.
        cache_key = f"bundle::{from_city}::{to_city}::{departure_date}::{destination}::{arguments.get('days') or ''}::{arguments.get('limit') or ''}"
        cached = _bundle_cache_get(cache_key)
        if cached is not None:
            return cached

        parts: list[str] = []
        errors: list[str] = []

        # run rail + weather in parallel. Sequential awaits used
        # to add 1-5s of dead time when the user wanted both. Whichever
        # finishes first lands first; gather waits for both.
        #
        # v0.45 — dropped ``and departure_date`` from the gate. The
        # rail handler now defaults to today when the date is missing,
        # and validates stations before deciding to error out — so
        # "南翔印象城到东方明珠" (non-rail) yields "unknown station"
        # instead of the misleading "缺出发日期" prompt.
        rail_coro = None
        if from_city and to_city:
            rail_coro = self._rail_12306({
                **arguments, "from_city": from_city, "to_city": to_city,
                "date": departure_date,
            })
        weather_coro = None
        if destination:
            weather_coro = self._weather_forecast({**arguments, "destination": destination})

        if rail_coro is not None and weather_coro is not None:
            rail, weather = await asyncio.gather(rail_coro, weather_coro)
        elif rail_coro is not None:
            rail = await rail_coro
            weather = None
        elif weather_coro is not None:
            rail = None
            weather = await weather_coro
        else:
            rail = None
            weather = None

        if rail is not None:
            if rail.ok and rail.content:
                parts.append(rail.content)
            else:
                errors.append(f"12306 查询失败：{rail.error or '无结果'}")
        elif from_city and to_city:
            # v0.45 — previously this branch fired "缺出发日期" even
            # when the user's from/to were unparsable as 12306
            # stations (e.g. "南翔印象城到东方明珠" — neither is a
            # rail station). The user would dutifully add a date and
            # still get nowhere. We now skip the date-missing nag
            # entirely; ``_rail_12306`` defaults to today and surfaces
            # a clearer "unknown station" error when applicable.
            errors.append(
                f"识别到路线 {from_city} → {to_city} 但没成功查到 12306 班次。"
                "（如果是市内/景区导航，可以直接告诉我用地图或公共交通查；"
                "12306 只覆盖铁路车站。）"
            )

        if weather is not None:
            if weather.ok and weather.content:
                parts.append(weather.content)
            else:
                errors.append(f"天气查询失败：{weather.error or '无结果'}")

        if not parts and errors:
            result = ToolResult(ok=False, content="", error="；".join(errors))
        elif not parts:
            result = ToolResult(
                ok=False, content="",
                error="未识别到可查询的路线/目的地。请提供出发地、目的地和日期。",
            )
        else:
            if errors:
                parts.append("⚠️ 实时查询异常\n" + "\n".join(f"- {e}" for e in errors))
            result = ToolResult(ok=True, content="\n\n".join(parts))

        if result.ok:
            _bundle_cache_put(cache_key, result)
        return result

    async def _rail_12306(self, arguments: dict[str, Any]) -> ToolResult:
        from_city = str(arguments.get("from_city") or "").strip()
        to_city = str(arguments.get("to_city") or "").strip()
        departure_date = _normalize_date(str(arguments.get("date") or ""))
        if not from_city or not to_city:
            return ToolResult(ok=False, content="", error="from_city and to_city are required")
        # v0.45 — when the user omits a date, default to today instead
        # of bouncing back a "date is required" error. People asking
        # "X 到 Y 怎么去 / 现在还有票吗" almost always mean today, and
        # the previous behaviour ("先告诉我日期") felt like the bot
        # didn't understand.
        if not departure_date:
            departure_date = datetime.now(SHANGHAI_TZ).date().isoformat()
        try:
            limit = int(arguments.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))
        only_available = bool(arguments.get("only_available", False))

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 ZLAgent/0.37",
            "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with httpx.AsyncClient(timeout=PER_CALL_TIMEOUT, follow_redirects=True, trust_env=True, headers=headers) as client:
            # warm 12306 session cookies. Without these the
            # leftTicket query returns an empty body and json.loads
            # fails with "Expecting value: line 1 column 1".
            try:
                await client.get(_LEFT_TICKET_INIT_URL)
            except httpx.HTTPError:
                # Best-effort: continue even on warmup failure; the
                # query itself will surface the real error if any.
                pass
            try:
                station_codes = await _load_station_codes(client)
            except Exception as exc:
                return ToolResult(ok=False, content="", error=f"failed to load 12306 station codes: {exc}")
            from_candidates = _candidate_stations(from_city, station_codes)
            to_candidates = _candidate_stations(to_city, station_codes)
            if not from_candidates:
                return ToolResult(ok=False, content="", error=f"unknown origin station/city: {from_city}")
            if not to_candidates:
                return ToolResult(ok=False, content="", error=f"unknown destination station/city: {to_city}")

            # top-2 by top-2 = 4 attempts max instead of 16. The
            # first preferred pair (e.g. 北京南→上海虹桥) succeeds for the
            # vast majority of intercity high-speed routes; the extra pairs
            # are kept only as a fallback for niche stations.
            rows: list[dict[str, Any]] = []
            sources: list[str] = []
            last_error = ""
            for from_name, from_code in from_candidates[:2]:
                for to_name, to_code in to_candidates[:2]:
                    fetched, station_map, source, err = await _fetch_rail_pair(client, departure_date, from_code, to_code)
                    if err:
                        last_error = err
                    if source:
                        sources.append(f"{from_name}({from_code})→{to_name}({to_code})/{source}")
                    for item in fetched:
                        parsed = _parse_rail_row(item, station_map)
                        if not parsed:
                            continue
                        if only_available and not _has_available_seat(parsed.get("seats", {})):
                            continue
                        key = (parsed.get("train"), parsed.get("from"), parsed.get("to"), parsed.get("start"))
                        if key not in {(r.get("train"), r.get("from"), r.get("to"), r.get("start")) for r in rows}:
                            rows.append(parsed)
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit:
                    break

            if not rows:
                return ToolResult(ok=False, content="", error=last_error or "no 12306 trains parsed for this route/date")
            rows.sort(key=lambda r: str(r.get("start") or ""))
            # fetch all train prices concurrently. Sequential
            # await on the price endpoint added ~0.5-2s per train; with
            # limit=8 the total dropped from ~10s to ~2s.
            await _attach_rail_prices(client, rows[:limit], departure_date)

        now = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
        lines = [
            "🚄 12306 实时余票",
            f"- 查询时间：{now} 北京时间",
            f"- 出发日期：{departure_date}",
            f"- 查询路线：{from_city} → {to_city}",
        ]
        if sources:
            lines.append(f"- 实际站点/接口：{'; '.join(sources[:3])}")
        lines.append("")
        for idx, row in enumerate(rows[:limit], 1):
            seats = _format_seats(row.get("seats", {}))
            status = row.get("status") or "状态未知"
            lines.append(
                f"{idx}. {row.get('train')}｜{row.get('from')} {row.get('start')} → "
                f"{row.get('to')} {row.get('arrive')}｜历时 {row.get('duration')}｜{status}"
            )
            if seats:
                lines.append(f"   座席：{seats}")
            prices = _format_prices(row.get("prices", {}))
            if prices:
                lines.append(f"   票价：{prices}")
        return ToolResult(ok=True, content="\n".join(lines), raw={"rows": rows[:limit], "sources": sources})

    async def _weather_forecast(self, arguments: dict[str, Any]) -> ToolResult:
        destination = str(arguments.get("destination") or arguments.get("to_city") or "").strip()
        if not destination:
            return ToolResult(ok=False, content="", error="destination is required")
        try:
            days = int(arguments.get("days") or 4)
        except (TypeError, ValueError):
            days = 4
        days = max(1, min(days, 16))
        async with httpx.AsyncClient(timeout=PER_CALL_TIMEOUT, follow_redirects=True, trust_env=True) as client:
            try:
                geo = await client.get(WEATHER_GEOCODE_URL, params={"name": destination, "count": 1, "language": "zh", "format": "json"})
                geo.raise_for_status()
                geo_data = geo.json()
                results = geo_data.get("results") or []
                if not results:
                    return ToolResult(ok=False, content="", error=f"Open-Meteo geocoding found no city: {destination}")
                place = results[0]
                lat = place.get("latitude")
                lon = place.get("longitude")
                forecast = await client.get(
                    WEATHER_FORECAST_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,snowfall,weather_code,wind_speed_10m,wind_direction_10m",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,rain_sum,wind_speed_10m_max,uv_index_max",
                        "forecast_days": days,
                        "timezone": "Asia/Shanghai",
                    },
                )
                forecast.raise_for_status()
                data = forecast.json()
            except httpx.HTTPError as exc:
                return ToolResult(ok=False, content="", error=f"Open-Meteo request failed: {exc}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return ToolResult(ok=False, content="", error=f"Open-Meteo parse failed: {exc}")

        current = data.get("current") or {}
        daily = data.get("daily") or {}
        place_name = place.get("name") or destination
        admin = place.get("admin1") or ""
        country = place.get("country") or ""
        now = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
        lines = [
            "🌦 当地实时天气 / 预报（Open-Meteo）",
            f"- 查询时间：{now} 北京时间",
            f"- 地点：{place_name}{(' · ' + admin) if admin else ''}{(' · ' + country) if country else ''}（{lat}, {lon}）",
            f"- 当前：{_weather_desc(current.get('weather_code'))}，气温 {current.get('temperature_2m')}°C，体感 {current.get('apparent_temperature')}°C，湿度 {current.get('relative_humidity_2m')}%",
            f"- 降水：{current.get('precipitation')} mm（雨 {current.get('rain')} / 雪 {current.get('snowfall')}），风速 {current.get('wind_speed_10m')} km/h，风向 {current.get('wind_direction_10m')}°",
            "",
            "未来几天：",
        ]
        times = daily.get("time") or []
        for i, day in enumerate(times[:days]):
            lines.append(
                f"- {day}：{_weather_desc(_pick(daily, 'weather_code', i))}，"
                f"{_pick(daily, 'temperature_2m_min', i)}-{_pick(daily, 'temperature_2m_max', i)}°C，"
                f"降水 {_pick(daily, 'precipitation_sum', i)} mm，"
                f"最大风速 {_pick(daily, 'wind_speed_10m_max', i)} km/h，"
                f"UV {_pick(daily, 'uv_index_max', i)}"
            )
        return ToolResult(ok=True, content="\n".join(lines), raw={"place": place, "current": current, "daily": daily})


async def _load_station_codes(client: httpx.AsyncClient) -> dict[str, str]:
    global _STATION_CODES
    if _STATION_CODES is not None:
        return _STATION_CODES
    resp = await client.get(STATION_NAME_URL)
    resp.raise_for_status()
    body = resp.text
    payload = body.split("'", 2)[1] if "'" in body else body
    mapping: dict[str, str] = {}
    for record in payload.split("@"):
        fields = record.split("|")
        if len(fields) >= 3 and fields[1] and fields[2]:
            mapping[fields[1]] = fields[2]
    _STATION_CODES = mapping
    return mapping


def _bundle_cache_get(key: str) -> Optional[ToolResult]:
    """Return a cached bundle ToolResult if still fresh; else None.

    two-layer lookup. L0 is the per-process dict (zero-network,
    truly hot), L1 is Redis (cross-worker, sub-ms LAN).  An L1 hit
    rehydrates L0 so subsequent reads on the same worker stay free.
    """
    entry = _BUNDLE_CACHE.get(key)
    if entry is not None:
        deadline, result = entry
        if time.monotonic() <= deadline:
            return result
        _BUNDLE_CACHE.pop(key, None)

    backend = _BUNDLE_REDIS_BACKEND
    if backend is None:
        return None
    redis_key = _bundle_redis_key(key)
    if not redis_key:
        return None
    payload = backend.get_json(redis_key)
    if not isinstance(payload, dict):
        return None
    try:
        result = ToolResult(
            ok=bool(payload.get("ok", True)),
            content=str(payload.get("content") or ""),
            error=str(payload.get("error") or "") or None,
        )
    except Exception:  # noqa: BLE001 - never fail a hit on serialization
        return None
    if not result.content:
        return None
    # Re-seed L0 so the same worker doesn't re-hit Redis on the next
    # call. Use the local TTL so the L0 entry expires independently.
    _BUNDLE_CACHE[key] = (time.monotonic() + _BUNDLE_CACHE_TTL, result)
    return result


def _bundle_cache_put(key: str, result: ToolResult) -> None:
    if len(_BUNDLE_CACHE) >= _BUNDLE_CACHE_MAX:
        # Drop the oldest deadline entry. ``min`` over the dict items is
        # cheap at this size (max 64); avoids a heap dependency.
        oldest_key = min(_BUNDLE_CACHE, key=lambda k: _BUNDLE_CACHE[k][0])
        _BUNDLE_CACHE.pop(oldest_key, None)
    _BUNDLE_CACHE[key] = (time.monotonic() + _BUNDLE_CACHE_TTL, result)

    backend = _BUNDLE_REDIS_BACKEND
    if backend is None:
        return
    redis_key = _bundle_redis_key(key)
    if not redis_key:
        return
    backend.set_json(
        redis_key,
        {
            "ok": bool(result.ok),
            "content": result.content,
            "error": result.error,
        },
        ttl_seconds=_BUNDLE_REDIS_TTL_SECONDS,
    )


def _candidate_stations(name: str, station_codes: dict[str, str]) -> list[tuple[str, str]]:
    clean = name.strip().replace("市", "")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    preferred = _CITY_STATION_PREFERENCES.get(clean, ())
    for station in (name, clean, *preferred):
        code = station_codes.get(station)
        if code and station not in seen:
            seen.add(station)
            out.append((station, code))
    if len(out) < 4:
        for station, code in station_codes.items():
            if station.startswith(clean) and station not in seen:
                seen.add(station)
                out.append((station, code))
            if len(out) >= 4:
                break
    return out


async def _fetch_rail_pair(client: httpx.AsyncClient, departure_date: str, from_code: str, to_code: str) -> tuple[list[str], dict[str, str], str, str]:
    params = {
        "leftTicketDTO.train_date": departure_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    last_error = ""
    for endpoint in RAIL_ENDPOINTS:
        url = f"https://kyfw.12306.cn/otn/leftTicket/{endpoint}"
        try:
            resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code} from 12306 {endpoint}"
                continue
            data = resp.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"12306 {endpoint} failed: {exc}"
            continue
        payload = data.get("data") or {}
        result = payload.get("result") or []
        station_map = payload.get("map") or {}
        if result:
            return list(result), dict(station_map), endpoint, ""
        message = data.get("messages") or data.get("message") or "no result"
        last_error = f"12306 {endpoint}: {message}"
    return [], {}, "", last_error


def _parse_rail_row(raw: str, station_map: dict[str, str]) -> Optional[dict[str, Any]]:
    fields = raw.split("|")
    if len(fields) < 33:
        return None
    seats = {
        "商务座": _seat_value(fields, 32),
        "一等座": _seat_value(fields, 31),
        "二等座": _seat_value(fields, 30),
        "软卧": _seat_value(fields, 23),
        "硬卧": _seat_value(fields, 28),
        "硬座": _seat_value(fields, 29),
        "无座": _seat_value(fields, 26),
    }
    button = fields[1] or ""
    can_buy = fields[11] if len(fields) > 11 else ""
    status = "可购" if can_buy == "Y" else (button or can_buy or "状态未知")
    return {
        "train_no": fields[2],
        "train": fields[3],
        "from": station_map.get(fields[6], fields[6]),
        "to": station_map.get(fields[7], fields[7]),
        "start": fields[8],
        "arrive": fields[9],
        "duration": fields[10],
        "status": status,
        "seats": seats,
        "from_station_no": fields[16] if len(fields) > 16 else "",
        "to_station_no": fields[17] if len(fields) > 17 else "",
        "seat_types": fields[35] if len(fields) > 35 else "",
    }


async def _attach_rail_prices(
    client: httpx.AsyncClient, rows: list[dict[str, Any]], departure_date: str,
) -> None:
    """Fan out one price-lookup per train concurrently. was
    sequential and added ~0.5-2s per row on the hot path; gather drops
    the total to roughly the slowest single call.
    """

    async def _one(row: dict[str, Any]) -> None:
        train_no = str(row.get("train_no") or "")
        from_station_no = str(row.get("from_station_no") or "")
        to_station_no = str(row.get("to_station_no") or "")
        seat_types = str(row.get("seat_types") or "")
        if not train_no or not from_station_no or not to_station_no or not seat_types:
            return
        try:
            resp = await client.get(
                "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice",
                params={
                    "train_no": train_no,
                    "from_station_no": from_station_no,
                    "to_station_no": to_station_no,
                    "seat_types": seat_types,
                    "train_date": departure_date,
                },
            )
            if resp.status_code >= 400:
                return
            payload = resp.json().get("data") or {}
            prices = _parse_price_payload(payload)
            if prices:
                row["prices"] = prices
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return

    if not rows:
        return
    await asyncio.gather(*(_one(row) for row in rows), return_exceptions=True)


def _parse_price_payload(payload: dict[str, Any]) -> dict[str, str]:
    mapping = {
        "A9": "商务座",
        "P": "特等座",
        "M": "一等座",
        "O": "二等座",
        "A6": "高级软卧",
        "A4": "软卧",
        "A3": "硬卧",
        "A1": "硬座",
        "WZ": "无座",
    }
    prices: dict[str, str] = {}
    for key, label in mapping.items():
        value = str(payload.get(key) or "").strip()
        if value and value not in {"--", "None", "null"}:
            prices[label] = value
    return prices


def _seat_value(fields: list[str], idx: int) -> str:
    if idx >= len(fields):
        return ""
    return (fields[idx] or "").strip()


def _format_seats(seats: dict[str, str]) -> str:
    parts = []
    for name, value in seats.items():
        if value and value not in {"*", "--"}:
            parts.append(f"{name}{value}")
    return "，".join(parts)


def _format_prices(prices: dict[str, str]) -> str:
    parts = []
    for name, value in prices.items():
        if value:
            parts.append(f"{name}{value}")
    return "，".join(parts)


def _has_available_seat(seats: dict[str, str]) -> bool:
    for value in seats.values():
        if value and value not in {"无", "--", "*"}:
            return True
    return False


def _normalize_date(value: str) -> str:
    text = value.strip()
    today = datetime.now(SHANGHAI_TZ).date()
    if not text:
        return ""
    if "后天" in text:
        return (today + timedelta(days=2)).isoformat()
    if "明天" in text:
        return (today + timedelta(days=1)).isoformat()
    if "今天" in text or "今日" in text:
        return today.isoformat()
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
    if m:
        candidate = date(today.year, int(m.group(1)), int(m.group(2)))
        if candidate < today:
            candidate = date(today.year + 1, int(m.group(1)), int(m.group(2)))
        return candidate.isoformat()
    return text if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text) else ""


def _safe_date(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _infer_from_query(query: str) -> dict[str, Any]:
    out: dict[str, Any] = {"query": query}
    text = query.strip()
    m = re.search(r"从(?P<from>[\u4e00-\u9fa5]{2,8})(?:出发)?[^\u4e00-\u9fa5]{0,6}(?:去|到)(?P<to>[\u4e00-\u9fa5]{2,8})", text)
    if not m:
        # v0.45 — added "怎么|如何|咋|远|近|多远|要多久|堵不堵" as
        # explicit terminators so cases like
        # "南翔印象城到东方明珠怎么去" no longer eat "怎么去" into the
        # ``to_city`` capture and produce phantom 12306 station names.
        m = re.search(
            r"(?P<from>[\u4e00-\u9fa5]{2,8})(?:去|到)"
            r"(?P<to>[\u4e00-\u9fa5]{2,8}?)"
            r"(?:\d|[一二两三四五六七八九十]+[天日]|旅游|旅行|攻略|坐|乘|天气"
            r"|怎么|如何|咋|远吗|近吗|多远|要多久|堵不堵|$)",
            text,
        )
    if m:
        out["from_city"] = _trim_city(m.group("from"))
        out["to_city"] = _trim_city(m.group("to"))
        out["destination"] = out["to_city"]
    if "destination" not in out:
        weather_city = re.search(
            r"(?P<city>[\u4e00-\u9fa5]{2,8})(?:今天|明天|后天)?(?:天气|气温|温度|下雨|降雨|风速)",
            text,
        )
        if weather_city:
            out["destination"] = _trim_city(weather_city.group("city"))
    inferred_date = _normalize_date(text)
    if inferred_date:
        out["date"] = inferred_date
    days = _infer_days(text)
    if days:
        out["days"] = days
    return out


def _trim_city(value: str) -> str:
    # v0.45 — extended trailing-suffix list so
    # "东方明珠怎么去" / "北京怎么走" / "上海如何到" / "陆家嘴咋去"
    # all trim to the bare city name. Previously only travel verbs
    # (出发/坐/乘/...) were stripped, leaving "X 怎么去" as the
    # captured city which then failed every downstream geocoder.
    cleaned = re.sub(
        r"(出发|明天|今天|后天|坐|乘|高铁|动车|火车|机票|航班|天气|攻略"
        r"|旅游|旅行|行程"
        r"|怎么(?:去|走|到|样)?|如何(?:去|走|到)?|咋(?:去|走|到)?"
        r"|哪里|哪儿|远吗|近吗|多远|要多久|堵不堵|能去吗)"
        r".*$",
        "",
        value,
    )
    return cleaned.strip(" ，,。.!！?？")


def _infer_days(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,2})\s*[天日]", text)
    if m:
        return int(m.group(1))
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.search(r"([一二两三四五六七八九十])\s*[天日]", text)
    if m:
        return cn.get(m.group(1))
    return None


def _weather_desc(code: Any) -> str:
    try:
        return _WEATHER_CODE_CN.get(int(code), f"天气代码 {code}")
    except (TypeError, ValueError):
        return "天气未知"


def _pick(data: dict[str, Any], key: str, idx: int) -> Any:
    value = data.get(key) or []
    if isinstance(value, list) and idx < len(value):
        return value[idx]
    return "?"

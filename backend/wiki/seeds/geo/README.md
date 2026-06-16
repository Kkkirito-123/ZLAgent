# Geo seed data

Static administrative-division data bundled with ZLAgent. Source-of-truth
JSON files; ETL'd into `geo_entities` SQLite table at startup by
`backend/wiki/geo_store.py:GeoStore.seed_from_json`.

## Files

| File | Rows | Notes |
|---|---|---|
| `provinces.json` | 34 | All省级行政区 (省/直辖市/自治区/特别行政区) |
| `cities.json` | ~30 | Hot 旅游 / 大型城市. Expand as needed. |
| `districts.json` (optional) | 0 (now) | District-level. Add later when needed. |

## Schema (per row)

```json
{
  "code": "110000",         // GB/T 2260 admin-division code (optional)
  "name": "北京市",          // canonical name (required)
  "short_name": "北京",      // for fuzzy match (required)
  "type": "province",        // country | province | city | district
  "parent_code": null,       // parent's code; null only for country root
  "aliases": ["京", "首都"], // optional alternate names
  "latitude": 39.9042,       // optional, WGS-84 decimal degrees
  "longitude": 116.4074
}
```

## Adding a new entry

1. Find the GB/T 2260 code: <https://www.mca.gov.cn/article/sj/xzqh/>
2. Append to the right JSON file. Keep them sorted by `code`.
3. Restart ZLAgent — the loader picks up additions on next boot.
4. To remove or rename: just delete the row, the loader is **upsert by
   `code`** so stale rows persist until you wipe `geo_entities`.

## Bulk import (optional, future)

To load all 3000+ districts, `git clone modood/Administrative-divisions-of-China`
into the workspace and run `python -m backend.wiki.geo_store --import-modood
<path>` (NOT YET IMPLEMENTED — file an issue when needed).

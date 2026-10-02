"""Tool handlers. Each takes the model's arguments and returns a JSON string, never raises."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata

from . import client, resolve

E_STAT_HOME = "https://www.e-stat.go.jp/"
TABLE_URL = "https://www.e-stat.go.jp/dbview?sid={id}"
CREDIT = (
    "このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、"
    "サービスの内容は国によって保証されたものではありません。"
)
SEARCH_FETCH = 500          # rows asked from getStatsList per statistic searched, then ranked here
DEFAULT_MAX_ROWS = 50
HARD_MAX_ROWS = 500
LATEST_FETCH_CELLS = 5000   # upper bound on cells pulled to find the latest period
DIM_LABELS = {"area": "地域", "time": "時点", "tab": "表章項目"}

# Everyday words -> the term e-Stat tables actually use.
SYNONYMS = {
    "給料": "賃金", "給与": "賃金", "年収": "賃金", "月給": "賃金", "時給": "賃金",
    "物価": "消費者物価指数", "cpi": "消費者物価指数", "インフレ": "消費者物価指数",
    "お店": "事業所数", "店舗数": "事業所数", "会社数": "企業数", "事業者数": "事業所数",
    "売上": "売上高", "人口数": "人口", "住民": "人口", "家族": "世帯",
    "失業": "完全失業率", "外国人": "外国人人口", "空き家": "空き家",
}
POPULAR = [
    "population / households: 国勢調査 (census, every 5 years, down to municipalities) or 人口推計 (yearly, prefectures)",
    "establishments / employees by industry: 経済センサス‐活動調査",
    "wages: 賃金構造基本統計調査; by prefecture × industry × age, query '都道府県別 年齢階級' "
    "(table 一般_都道府県別_年齢階級別DB)",
    "consumer prices: 消費者物価指数 (headline 総合; 'core' = 生鮮食品を除く総合)",
]


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _error(message: str, **extra) -> str:
    return _dump({"error": message, **extra})


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("$", ""))
    return "" if value is None else str(value)


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _guard(handler):
    def wrapped(args, **kwargs):
        try:
            with client.budget():
                return handler(args or {})
        except client.AppIdError as error:
            return _error(str(error), kind="app_id_missing_or_invalid")
        except client.EstatError as error:
            return _error(str(error), kind=error.kind)
        except Exception as error:  # never let a tool call crash the agent
            detail = client._scrub(f"{type(error).__name__}: {error}", os.environ.get(client.APP_ID_ENV, ""))
            return _error(f"Unexpected error in the e-Stat plugin ({detail}). Please report it with the "
                          "arguments you used.", kind="internal_error")
    wrapped.__name__ = handler.__name__
    wrapped.__doc__ = handler.__doc__
    return wrapped


# --- shared formatting ----------------------------------------------------------


def _survey_date(value) -> str | None:
    text = _text(value).strip()
    if not text or text == "0":
        return None
    def one(part):
        part = part.strip()
        if re.fullmatch(r"\d{6}", part):
            return f"{part[:4]}-{part[4:]}"
        return part
    return " to ".join(one(p) for p in text.split("-"))


def _statistic_name(table: dict) -> str:
    name = _text(table.get("STATISTICS_NAME"))
    return re.sub(r"[\s　]*（主な内容：[^）]*）", "", name).strip()


def _title(table: dict) -> str:
    title = table.get("TITLE")
    number = title.get("@no") if isinstance(title, dict) else None
    text = _text(title)
    return f"{number} {text}".strip() if number else text


def _base_year(name: str) -> int | None:
    m = re.search(r"(\d{4})年基準", name)
    if m:
        return int(m.group(1))
    m = re.search(r"平成(\d+)年基準", name)
    if m:
        return 1988 + int(m.group(1))
    return None


def _coverage(table: dict) -> str | None:
    """Area coverage from the title: '…男女別人口－全国，都道府県，市区町村' -> '全国，都道府県，市区町村'."""
    parts = re.split(r"\s*－\s*", _text(table.get("TITLE")))
    return parts[-1].strip() if len(parts) > 1 and parts[-1].strip() else None


_ERA = {"令和": 2018, "平成": 1988, "昭和": 1925}


def _data_year(table: dict) -> int | None:
    """The year the numbers are about: survey date, else a year in the names, else the update year."""
    m = re.match(r"(\d{4})", _text(table.get("SURVEY_DATE")))
    if m and m.group(1) != "0000":
        return int(m.group(1))
    names = unicodedata.normalize("NFKC", _statistic_name(table) + " " + _title(table))
    updated = int(str(table["UPDATED_DATE"])[:4]) if table.get("UPDATED_DATE") else None
    if re.search(r"以降|年[~～]\s*(?:$|\s|[|）)])", names):
        return updated
    years = [int(y) for y in re.findall(r"((?:19|20)\d{2})年", names)]
    years += [_ERA[e] + (1 if n == "元" else int(n)) for e, n in re.findall(r"(令和|平成|昭和)(\d+|元)年", names)]
    return max(years) if years else updated


def _table_flags(table: dict) -> list[str]:
    stat, title = _statistic_name(table), _title(table)
    notes = []
    if "速報" in stat or "速報" in title:
        notes.append("preliminary (速報): the final (確定) release usually differs; prefer a 確定 table if one exists")
    if "不詳補完" in stat or "不詳補完" in title:
        notes.append("imputed (不詳補完値): unknown answers were filled in statistically")
    if "参考表" in stat or "参考表" in title:
        notes.append("reference table (参考表)")
    return notes


def _header(table: dict) -> dict:
    tid = table.get("@id", "")
    out = {
        "id": tid,
        "statistic": _statistic_name(table),
        "title": _title(table),
        "organization": _text(table.get("GOV_ORG")),
        "survey_date": _survey_date(table.get("SURVEY_DATE")),
        "posted_on_e_stat": table.get("OPEN_DATE"),
        "updated_on_e_stat": table.get("UPDATED_DATE"),
        "url": TABLE_URL.format(id=tid),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def _source(header: dict) -> dict:
    org = header.get("organization", "")
    name = f"「{header.get('statistic', '')} {header.get('title', '')}」".replace("  ", " ")
    citation = f"出典：政府統計の総合窓口(e-Stat)（{E_STAT_HOME}）{name}（{org}）"
    return {
        "citation": citation,
        "citation_if_edited": f"{name}（{org}）（政府統計の総合窓口(e-Stat)）を加工して作成",
        "table_url": header.get("url"),
        "credit": CREDIT,
    }


def _dimensions(meta: dict) -> list[dict]:
    objs = _as_list(meta.get("METADATA_INF", {}).get("CLASS_INF", {}).get("CLASS_OBJ"))
    dims = []
    for obj in objs:
        items = []
        for c in _as_list(obj.get("CLASS")):
            items.append({
                "code": str(c.get("@code", "")),
                "name": str(c.get("@name", "")),
                "level": str(c.get("@level", "") or ""),
                "unit": c.get("@unit") or "",
                "parent": c.get("@parentCode"),
            })
        dims.append({"id": obj.get("@id", ""), "name": obj.get("@name", ""), "items": items})
    return dims


def _label(dim: dict) -> str:
    if dim["id"] in DIM_LABELS:
        return DIM_LABELS[dim["id"]]
    return dim["name"] or dim["id"]


def _item_text(item: dict, by_code: dict, dim_id: str) -> str:
    extra = [item["code"]]
    if dim_id == "area" and item.get("parent") in by_code and item.get("level") not in ("", "1", "2"):
        extra.append(by_code[item["parent"]]["name"])
    if item.get("unit"):
        extra.append(item["unit"])
    return f"{item['name']} ({', '.join(extra)})"


def _ordered(dim: dict) -> list[dict]:
    if dim["id"] == "time":
        return sorted(dim["items"], key=lambda it: it["code"], reverse=True)
    return dim["items"]


# --- estat_search_tables ----------------------------------------------------------


def _search_word(query: str) -> str:
    q = re.sub(r"[\s　]+", " ", query).strip()
    if re.search(r"\b(AND|OR|NOT)\b", q):
        return q
    return " AND ".join(q.split(" "))


def _survey_years(year: str) -> str | None:
    text = resolve.norm(year)
    if not text:
        return None
    parts = re.split(r"[~～]|(?<=\d)-(?=\d{4})|to", text)
    def one(part, end=False):
        targets = resolve.time_targets(part)
        t = targets[0] if targets else part
        m = re.match(r"^(\d{4})年(?:(\d{1,2})月)?", t)
        if not m:
            m2 = re.match(r"^(\d{4})(\d{2})?$", part)
            if not m2:
                raise ValueError(year)
            y, mo = m2.group(1), m2.group(2)
        else:
            y, mo = m.group(1), m.group(2)
        if mo:
            return f"{y}{int(mo):02d}"
        return f"{y}{'12' if end else '01'}" if len(parts) > 1 else y
    if len(parts) == 1:
        return one(parts[0])
    return f"{one(parts[0])}-{one(parts[-1], end=True)}"


def _survey_codes(survey: str) -> tuple[list[str], str | None]:
    """Survey name or code -> statsCode values (at most 3)."""
    survey = survey.strip()
    if re.fullmatch(r"\d{5}|\d{8}", survey):
        return [survey], None
    root = client.call("getStatsList", {"statsNameList": "Y", "searchWord": survey})
    names = _as_list(root.get("DATALIST_INF", {}).get("LIST_INF"))
    key = resolve.norm(survey)
    hits = [n for n in names if key in resolve.norm(_text(n.get("STAT_NAME")))]
    if not hits:
        return [], f"no statistic is named like '{survey}', so it was used as an extra keyword"
    note = None
    if len(hits) > 3:
        note = "survey matched several statistics; searched the first 3: " + ", ".join(
            _text(h.get("STAT_NAME")) for h in hits[:3])
    return [str(h.get("@id")) for h in hits[:3]], note


def _score(table: dict, words: list[str]) -> float:
    title = resolve.norm(_title(table))
    stat = resolve.norm(_statistic_name(table))
    score = 0.0
    for w in words:
        w = resolve.norm(w)
        if not w:
            continue
        if w in title:
            score += 3
        elif w in stat:
            score += 1.5
    flags = " ".join(_table_flags(table))
    if "preliminary" in flags:
        score -= 4
    if "imputed" in flags or "reference" in flags:
        score -= 2
    # The dimension list sits before '－'; long cross-tabulations are harder to use.
    head = re.split(r"[－-]", _text(table.get("TITLE")))[0]
    score -= 0.4 * len(re.findall(r"[、，,]", head))
    # After '－' comes the area coverage. Standard geography beats special regions.
    tail = _coverage(table) or ""
    score += 0.8 * ("都道府県" in tail) + 0.8 * ("市区町村" in tail) + 0.4 * ("全国" in tail)
    if any(w in tail for w in ("都市圏", "人口集中地区", "メッシュ", "小地域")):
        score -= 1.5
    # Basic tabulations and the first tables of a release are the ones most people want.
    if "基本集計" in stat:
        score += 0.5
    number = table.get("TITLE", {}).get("@no", "") if isinstance(table.get("TITLE"), dict) else ""
    if re.match(r"^1(?:\D|$)", str(number)):
        score += 0.5
    year = _data_year(table)
    if year:
        score += max(0.0, min(4.0, (year - 2000) / 26 * 4))
        if year < time.localtime().tm_year - 10:
            score -= 1.0
    cells = table.get("OVERALL_TOTAL_NUMBER") or 0
    try:
        score -= 0.15 * math.log10(max(1, int(cells)))
    except (TypeError, ValueError):
        pass
    return score


def _merge_tables(raw: list[dict]) -> list[dict]:
    """getStatsList repeats a table once per area level; fold the repeats into one entry."""
    merged: dict[str, dict] = {}
    for t in raw:
        tid = t.get("@id")
        if tid in merged:
            area = t.get("COLLECT_AREA")
            if area and area not in merged[tid]["_areas"]:
                merged[tid]["_areas"].append(area)
            continue
        t = dict(t)
        t["_areas"] = [t["COLLECT_AREA"]] if t.get("COLLECT_AREA") else []
        merged[tid] = t
    return list(merged.values())


def _mark_superseded(tables: list[dict]) -> None:
    by_stat: dict[str, list[tuple[int, dict]]] = {}
    for t in tables:
        base = _base_year(_statistic_name(t))
        if base:
            by_stat.setdefault(_text(t.get("STAT_NAME")), []).append((base, t))
    for group in by_stat.values():
        newest = max(b for b, _ in group)
        newest_ids = [t.get("@id") for b, t in group if b == newest]
        for base, t in group:
            if base < newest:
                t["_superseded"] = (
                    f"older base year ({base}年基準); the current official series is {newest}年基準 "
                    f"(table {', '.join(newest_ids[:2])})"
                )


@_guard
def search_tables(args: dict) -> str:
    """estat_search_tables"""
    query = str(args.get("query") or "").strip()
    if not query:
        return _error("query is empty. Give Japanese keywords such as '人口 世帯数', '事業所数 産業', "
                      "'賃金 産業', or '消費者物価指数'.", popular_sources=POPULAR)
    limit = max(1, min(int(args.get("limit") or 10), 30))
    survey = str(args.get("survey") or "").strip()
    year = str(args.get("year") or "").strip()
    area_level = str(args.get("area_level") or "").strip().lower()
    area_map = {"national": "1", "prefecture": "2", "municipality": "3",
                "全国": "1", "都道府県": "2", "市区町村": "3", "1": "1", "2": "2", "3": "3"}
    if area_level and area_level not in area_map:
        return _error(f"area_level '{area_level}' is not one of national, prefecture, municipality.")
    notes = []
    words = re.sub(r"\b(AND|OR|NOT)\b", " ", re.sub(r"[\s　]+", " ", query)).split()
    params = {"searchWord": _search_word(query), "limit": SEARCH_FETCH, "explanationGetFlg": "N"}
    if year:
        try:
            params["surveyYears"] = _survey_years(year)
        except ValueError:
            return _error(f"year '{year}' was not understood. Use '2020', '令和2年', '2020-2023' or '202010'.")
    if area_level:
        params["collectArea"] = area_map[area_level]
    codes = [None]
    if survey:
        found, note = _survey_codes(survey)
        if note:
            notes.append(note)
        if found:
            codes = found
        else:
            params["searchWord"] = _search_word(f"{query} {survey}")
            words.append(survey)

    raw, total, complete = [], 0, True
    for code in codes:
        p = dict(params)
        if code:
            p["statsCode"] = code
        root = client.call("getStatsList", p)
        info = root.get("DATALIST_INF", {})
        total += int(info.get("NUMBER") or 0)
        raw.extend(_as_list(info.get("TABLE_INF")))
        if (info.get("RESULT_INF") or {}).get("NEXT_KEY"):
            complete = False

    if not raw:
        return _dump(_no_hits(query, words, params, survey, notes))

    tables = _merge_tables(raw)
    _mark_superseded(tables)
    for t in tables:
        t["_score"] = _score(t, words) - (4 if t.get("_superseded") else 0)
    tables.sort(key=lambda t: -t["_score"])

    out_tables = []
    for t in tables[:limit]:
        entry = _header(t)
        coverage = _coverage(t)
        areas = [a for a in t["_areas"] if a and a != "該当なし"]
        if coverage:
            entry["areas"] = coverage
        elif areas:
            entry["areas"] = "，".join(areas)
        if t.get("CYCLE") and t.get("CYCLE") != "-":
            entry["cycle"] = t["CYCLE"]
        entry["values_in_table"] = t.get("OVERALL_TOTAL_NUMBER")
        flags = _table_flags(t) + ([t["_superseded"]] if t.get("_superseded") else [])
        if flags:
            entry["notes"] = flags
        out_tables.append(entry)

    result = {
        "total_matches": len(tables) if complete else total,
        "shown": len(out_tables),
        "tables": out_tables,
        "next_step": ("Pick the table whose title fits, then call estat_table_info(table_id) to see its "
                      "dimensions, or estat_get_data(table_id, area=..., time=...) directly."),
    }
    if not complete:
        result["narrowing_tip"] = (
            f"e-Stat reports about {total} matches; these are the best of the first {len(tables)} tables. To narrow: add a word "
            "from the title you expect, set survey (e.g. '国勢調査'), year (e.g. '2020') or area_level.")
    if notes:
        result["notes"] = notes
    return _dump(result)


def _no_hits(query, words, params, survey, notes) -> dict:
    result = {"total_matches": 0, "tables": []}
    tips = []
    relaxed = {k: v for k, v in params.items() if k in ("searchWord", "limit", "explanationGetFlg")}
    if len(relaxed) < len(params) or survey:
        root = client.call("getStatsList", dict(relaxed, limit=1))
        n = int(root.get("DATALIST_INF", {}).get("NUMBER") or 0)
        if n:
            tips.append(f"{n} tables match the keywords without the survey/year/area_level filters. Many "
                        "long-running series (prices, wages) carry no survey year, so drop year for those.")
    swaps = [f"'{w}' -> '{SYNONYMS[resolve.norm(w)]}'" for w in words if resolve.norm(w) in SYNONYMS]
    if swaps:
        tips.append("Use the official term: " + ", ".join(swaps) + ".")
    if len(words) > 1:
        tips.append("All words must appear; try fewer words (e.g. just the main one) or join with OR.")
    else:
        tips.append("Try a broader official word, e.g. '人口' rather than a full phrase.")
    tips.append("Write keywords in Japanese; e-Stat titles are Japanese.")
    result["message"] = f"No e-Stat table matches '{query}'."
    result["try_next"] = tips
    result["popular_sources"] = POPULAR
    if notes:
        result["notes"] = notes
    return result


# --- estat_table_info -------------------------------------------------------------


def _table_id(args: dict) -> str:
    tid = re.sub(r"\D", "", str(args.get("table_id") or args.get("statsDataId") or ""))
    if not tid:
        raise client.EstatError(
            "table_id is missing. Get one (10 digits, e.g. 0003445078) from estat_search_tables.",
            kind="bad_request")
    return tid


def _meta(table_id: str) -> dict:
    try:
        return client.get_meta(table_id)
    except client.EstatError as error:
        if error.kind in ("not_found", "bad_request"):
            raise client.EstatError(
                f"There is no e-Stat table with id {table_id}. Table ids are 10 digits; "
                "find one with estat_search_tables.", kind="not_found") from None
        raise


def _level_summary(dim: dict) -> dict:
    levels: dict[str, list] = {}
    for it in dim["items"]:
        levels.setdefault(it["level"] or "-", []).append(it)
    if len(levels) <= 1:
        return {}
    return {lv: f"{len(its)} item(s), e.g. {its[0]['name']}" for lv, its in sorted(levels.items())}


def _example_call(table_id: str, dims: list[dict]) -> dict:
    example = {"table_id": table_id}
    filters = {}
    for d in dims:
        if d["id"] == "area":
            names = [it["name"] for it in d["items"]]
            example["area"] = next((n for n in ("東京都", "東京都区部", "13100 東京都区部") if n in names),
                                   "全国" if "全国" in names else names[0])
        elif d["id"] == "time":
            example["time"] = "latest"
        elif d["id"].startswith("cat") and len(d["items"]) > 1:
            total = resolve.total_item(d["items"])
            if total:
                filters[d["name"]] = total["name"]
    if filters:
        example["filters"] = filters
    return example


@_guard
def table_info(args: dict) -> str:
    """estat_table_info"""
    table_id = _table_id(args)
    meta = _meta(table_id)
    header = _header(meta.get("METADATA_INF", {}).get("TABLE_INF", {}))
    dims = _dimensions(meta)
    wanted = str(args.get("dimension") or "").strip()
    search = str(args.get("search") or "").strip()
    if wanted:
        dim = resolve.find_dimension(wanted, dims)
        if not dim:
            return _error(f"This table has no dimension called '{wanted}'.",
                          dimensions=[{"key": d["id"], "name": d["name"], "size": len(d["items"])} for d in dims])
        selected = [dim]
        per_dim = max(1, min(int(args.get("max_items") or 100), 300))
    else:
        selected = dims
        per_dim = max(1, min(int(args.get("max_items") or 12), 100))

    out_dims = []
    for d in selected:
        by_code = {it["code"]: it for it in d["items"]}
        items = _ordered(d)
        if search:
            key = resolve.bare(search)
            items = [it for it in items if key in resolve.bare(it["name"]) or it["code"] == search]
            if not items and not wanted:
                continue
        entry = {"key": d["id"], "name": d["name"], "size": len(d["items"])}
        levels = _level_summary(d)
        if levels and not search:
            entry["levels"] = levels
        entry["items"] = [_item_text(it, by_code, d["id"]) for it in items[:per_dim]]
        if len(items) > per_dim:
            entry["more"] = (f"{len(items) - per_dim} more. List them with dimension='{d['id']}' and "
                             "search='<part of a name>' (or max_items up to 300).")
        if d["id"] == "time" and len(d["items"]) > 1:
            codes = sorted(it["code"] for it in d["items"])
            entry["range"] = f"{by_code[codes[0]]['name']} … {by_code[codes[-1]]['name']}"
        out_dims.append(entry)

    if search and not any(e["items"] for e in out_dims):
        return _error(f"No item in {'this dimension' if wanted else 'any dimension'} contains '{search}'. "
                      "Try a shorter part of the name, or omit search to see the first items.")
    result = {"table": header, "dimensions": out_dims}
    flags = _table_flags(meta.get("METADATA_INF", {}).get("TABLE_INF", {}))
    if flags:
        result["notes"] = flags
    result["next_step"] = ("Call estat_get_data with names from these lists (codes also work). "
                           "Unset classifications default to their total (総数 etc.), area to 全国, time to latest.")
    result["example"] = _example_call(table_id, dims)
    return _dump(result)


# --- estat_get_data ---------------------------------------------------------------


_PARAM_SUFFIX = {"tab": "Tab", "time": "Time", "area": "Area"}


def _param_name(dim_id: str) -> str:
    if dim_id in _PARAM_SUFFIX:
        return _PARAM_SUFFIX[dim_id]
    m = re.fullmatch(r"cat(\d+)", dim_id)
    return f"Cat{m.group(1)}" if m else dim_id[:1].upper() + dim_id[1:]


class _Selection:
    def __init__(self, dim):
        self.dim = dim
        self.codes: list[str] | None = None   # None = every item
        self.level: str | None = None
        self.latest: tuple[int, str | None] | None = None
        self.default_note: str | None = None


def _select(dim: dict, value, issues: list) -> _Selection:
    sel = _Selection(dim)
    items = dim["items"]
    by_code = {it["code"]: it for it in items}
    raw_values = value if isinstance(value, list) else [value]
    # A name may itself contain a comma; try the whole string before splitting it.
    values = []
    for v in raw_values:
        v = str(v).strip()
        try:
            resolve.match_item(v, items, dim["id"])
            values.append(v)
        except (resolve.Ambiguous, resolve.NoMatch):
            values.extend(resolve.split_values(v))
    codes: list[str] = []
    for v in values:
        if resolve.norm(v) in resolve.ALL_VALUES:
            sel.codes = None
            return sel
        if dim["id"] == "time":
            latest = resolve.parse_latest(v)
            if latest:
                sel.latest = latest
                continue
        # A plain name first: item names may themselves contain '～' or ':'.
        pending = None
        try:
            codes.append(resolve.match_item(v, items, dim["id"])["code"])
            continue
        except resolve.Ambiguous as error:
            pending = _ambiguous(dim, error, by_code)
        except resolve.NoMatch as error:
            pending = _nomatch(dim, error, by_code)
        m = re.match(r"^(?:level|lv|階層)\s*[:：=]\s*(\d+)$", v, re.I)
        if m:
            lv = m.group(1)
            hits = [it["code"] for it in items if it["level"] == lv]
            if not hits:
                issues.append({"dimension": _label(dim), "value": v,
                               "problem": f"no items at level {lv}",
                               "levels": _level_summary(dim)})
            codes.extend(hits)
            continue
        m = re.match(r"^(?:children|内訳|下位)\s*[:：=]\s*(.+)$", v, re.I) or re.match(r"^(.+?)の(?:内訳|下位|市区町村)$", v)
        if m:
            try:
                parent = resolve.match_item(m.group(1), items, dim["id"])
            except resolve.Ambiguous as error:
                issues.append(_ambiguous(dim, error, by_code))
                continue
            except resolve.NoMatch as error:
                issues.append(_nomatch(dim, error, by_code))
                continue
            kids = [it["code"] for it in items if it.get("parent") == parent["code"]]
            if not kids:
                issues.append({"dimension": _label(dim), "value": v,
                               "problem": f"{parent['name']} has no sub-items in this table"})
            codes.extend(kids)
            continue
        rng = re.split(r"\s*(?:~|～|〜|\.\.|\bto\b)\s*", v)
        if len(rng) == 2 and all(rng):
            try:
                a = resolve.match_item(rng[0], items, dim["id"])
                b = resolve.match_item(rng[1], items, dim["id"])
            except resolve.Ambiguous as error:
                issues.append(_ambiguous(dim, error, by_code))
                continue
            except resolve.NoMatch as error:
                issues.append(_nomatch(dim, error, by_code))
                continue
            lo, hi = sorted([a["code"], b["code"]])
            if dim["id"] == "time":
                gran = {resolve.time_granularity(a["name"]), resolve.time_granularity(b["name"])}
                codes.extend(it["code"] for it in items if lo <= it["code"] <= hi
                             and resolve.time_granularity(it["name"]) in gran)
            else:
                codes.extend(it["code"] for it in items if lo <= it["code"] <= hi)
            continue
        issues.append(pending)
    if codes:
        seen = set()
        sel.codes = [c for c in codes if not (c in seen or seen.add(c))]
    elif sel.latest is None and not issues:
        sel.codes = None
    return sel


def _ambiguous(dim, error, by_code) -> dict:
    cands = error.candidates[:10]
    same_name = len({resolve.bare(it["name"]) for it in error.candidates}) == 1
    if same_name and dim["id"] == "area":
        parent = by_code.get(cands[0].get("parent") or "", {}).get("name", "東京都")
        how = f"pass the code, or put the prefecture first (e.g. '{parent}{cands[0]['name']}')"
    elif same_name:
        codes = ",".join(it["code"] for it in cands)
        how = (f"the table has {len(error.candidates)} items with exactly this name; pass one code, or "
               f"'{codes}' to see them side by side (the table page explains the difference)")
    else:
        how = "pass the exact name or the code"
    if same_name and dim["id"] != "area":
        problem = f"'{error.value}': {how}"
    else:
        problem = f"'{error.value}' matches {len(error.candidates)} items; {how}"
    return {
        "dimension": _label(dim), "value": error.value, "problem": problem,
        "candidates": [_item_text(it, by_code, dim["id"]) for it in cands],
    }


def _nomatch(dim, error, by_code) -> dict:
    entry = {
        "dimension": _label(dim), "value": error.value,
        "problem": f"no item named '{error.value}' in this table",
    }
    if error.suggestions:
        entry["closest"] = [_item_text(it, by_code, dim["id"]) for it in error.suggestions]
    elif dim["id"] == "area":
        entry["problem"] += "; nothing similar either. Areas are Japanese place names such as 東京都 or 札幌市"
    entry["see_all"] = f"estat_table_info(table_id, dimension='{dim['id']}', search='<part of the name>')"
    return entry


def _number(text: str):
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    return None


_newer_base_cache: dict[str, tuple[float, list]] = {}
_newer_base_lock = threading.Lock()


def _newer_base_warning(table: dict) -> str | None:
    base = _base_year(_statistic_name(table))
    stat_code = (table.get("STAT_NAME") or {}).get("@code") if isinstance(table.get("STAT_NAME"), dict) else None
    if not base or not stat_code:
        return None
    with _newer_base_lock:
        hit = _newer_base_cache.get(stat_code)
    if hit and time.monotonic() - hit[0] < 3600:
        listed = hit[1]
    else:
        try:
            root = client.call("getStatsList", {"statsCode": stat_code, "limit": 100, "explanationGetFlg": "N"})
        except client.EstatError:
            return (f"This table uses the {base}年基準 (base year {base}). The plugin could not check whether "
                    "a newer base year exists (e-Stat did not answer in time); if one does, current press "
                    "releases use it and the values here will differ.")
        listed = [(_base_year(_statistic_name(t)), t.get("@id"))
                  for t in _as_list(root.get("DATALIST_INF", {}).get("TABLE_INF"))]
        with _newer_base_lock:
            _newer_base_cache[stat_code] = (time.monotonic(), listed)
    newer = sorted({(b, i) for b, i in listed if b and b > base}, reverse=True)
    if not newer:
        return None
    top = newer[0][0]
    ids = [i for b, i in newer if b == top][:2]
    return (f"This table uses the {base}年基準 (base year {base}). A newer {top}年基準 series exists "
            f"(table {', '.join(ids)}); official figures published now use the newer base, so the "
            "values here will not match current press releases.")


@_guard
def get_data(args: dict) -> str:
    """estat_get_data"""
    table_id = _table_id(args)
    max_rows = max(1, min(int(args.get("max_rows") or DEFAULT_MAX_ROWS), HARD_MAX_ROWS))
    meta = _meta(table_id)
    table = meta.get("METADATA_INF", {}).get("TABLE_INF", {})
    header = _header(table)
    dims = _dimensions(meta)
    by_id = {d["id"]: d for d in dims}

    filters = args.get("filters") or {}
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            return _error("filters must be an object such as {\"男女\": \"総数\"}.")
    if not isinstance(filters, dict):
        return _error("filters must be an object such as {\"男女\": \"総数\"}.")
    requested = dict(filters)
    for key in ("area", "time"):
        if args.get(key) not in (None, "", []):
            requested[key] = args[key]

    selections: dict[str, _Selection] = {}
    explicit: set[str] = set()
    issues: list[dict] = []
    for key, value in requested.items():
        dim = resolve.find_dimension(str(key), dims)
        if dim is None:
            if key == "area" or key == "time":
                issues.append({"dimension": key, "value": value,
                               "problem": f"this table has no {key} dimension; leave {key} out"})
                continue
            return _error(
                f"This table has no dimension called '{key}'.",
                dimensions=[{"key": d["id"], "name": d["name"], "size": len(d["items"])} for d in dims],
                next_step=(f"Use one of these keys or names in filters. If you need a breakdown by '{key}', "
                           f"this table does not have it: search for a table whose title mentions {key} "
                           "with estat_search_tables."))
        selections[dim["id"]] = _select(dim, value, issues)
        explicit.add(dim["id"])

    if issues:
        return _dump({
            "error": "Some names did not match exactly one item. Pick from the candidates and call again.",
            "kind": "needs_choice",
            "table": {"id": table_id, "title": header.get("title")},
            "issues": issues,
        })

    defaults = {}
    for d in dims:
        if d["id"] in selections or len(d["items"]) <= 1:
            continue
        sel = _Selection(d)
        if d["id"] == "time":
            sel.latest = (1, None)
            defaults[_label(d)] = "latest period with data"
        elif d["id"] == "area":
            nat = next((it for it in d["items"] if resolve.bare(it["name"]) == "全国"), None)
            if nat:
                sel.codes = [nat["code"]]
                defaults[_label(d)] = nat["name"]
        elif d["id"].startswith("cat"):
            total = resolve.total_item(d["items"])
            if total:
                sel.codes = [total["code"]]
                defaults[_label(d)] = total["name"]
        selections[d["id"]] = sel

    # Time window for 'latest': newest codes of the wanted granularity.
    window: list[str] | None = None
    time_sel = selections.get("time")
    if time_sel and time_sel.latest:
        n, gran = time_sel.latest
        tdim = by_id["time"]
        grans = {}
        for it in tdim["items"]:
            grans.setdefault(resolve.time_granularity(it["name"]), []).append(it)
        if gran is None:
            gran = next(g for g in resolve.GRANULARITY_ORDER if g in grans)
        if gran not in grans:
            return _error(f"This table has no {gran.replace('_', ' ')} periods. Available: "
                          + ", ".join(sorted(grans)) + ". Use time='latest' or a period name.")
        ordered = sorted(grans[gran], key=lambda it: it["code"], reverse=True)
        others = 1
        for did, d in by_id.items():
            if did == "time":
                continue
            s = selections.get(did)
            others *= len(s.codes) if s and s.codes is not None else len(d["items"])
        size = max(n + 2, min(100, n * 2 + 12, LATEST_FETCH_CELLS // max(1, others)))
        window = [it["code"] for it in ordered[:size]]
        time_sel.codes = window

    params = {"statsDataId": table_id, "metaGetFlg": "N", "explanationGetFlg": "N", "annotationGetFlg": "Y"}
    client_filter: dict[str, set] = {}
    estimate = 1        # upper bound on rows returned to the model
    fetch_estimate = 1  # upper bound on cells pulled from e-Stat (includes the latest-window)
    for d in dims:
        s = selections.get(d["id"])
        codes = s.codes if s else None
        name = _param_name(d["id"])
        if codes is None:
            estimate *= len(d["items"])
            fetch_estimate *= len(d["items"])
            continue
        if not codes:
            return _error(f"Nothing selected in {_label(d)}.")
        estimate *= min(len(codes), s.latest[0]) if (s.latest and window) else len(codes)
        fetch_estimate *= len(codes)
        if len(codes) <= 100:
            params[f"cd{name}"] = ",".join(codes)
        else:
            params[f"cd{name}From"], params[f"cd{name}To"] = min(codes), max(codes)
            client_filter[d["id"]] = set(codes)

    count = None
    need_count = (fetch_estimate > LATEST_FETCH_CELLS if window else estimate > max_rows) or bool(client_filter)
    if need_count:
        root = client.call("getStatsData", dict(params, cntGetFlg="Y"))
        count = int(root.get("STATISTICAL_DATA", {}).get("RESULT_INF", {}).get("TOTAL_NUMBER") or 0)
        if window:
            rows_guess = count * time_sel.latest[0] // max(1, len(window))
            too_many = count > LATEST_FETCH_CELLS
        else:
            rows_guess = count
            too_many = count > max_rows and (not client_filter or count > 20000)
        if too_many:
            return _dump(_needs_narrowing(table_id, header, dims, selections, rows_guess, max_rows, defaults))

    rows_raw = []
    if count != 0:
        want = max(max_rows, count or 0, fetch_estimate if window else 0) + 1
        root = client.call("getStatsData", dict(params, limit=min(100000, want)))
        sd = root.get("STATISTICAL_DATA", {})
        data = sd.get("DATA_INF", {})
        rows_raw = _as_list(data.get("VALUE"))
        notes_def = {str(n.get("@char")): _text(n) for n in _as_list(data.get("NOTE"))}
    else:
        notes_def = {}

    for did, allowed in client_filter.items():
        rows_raw = [r for r in rows_raw if r.get(f"@{did}") in allowed]

    extra_warnings: list[str] = []
    if window and time_sel:
        n = time_sel.latest[0]
        with_data = sorted({r.get("@time") for r in rows_raw if _number(str(r.get("$", ""))) is not None},
                           reverse=True)
        filled = {}
        for r in rows_raw:
            if _number(str(r.get("$", ""))) is not None:
                filled[r.get("@time")] = filled.get(r.get("@time"), 0) + 1
        # 'latest' = newest periods that have a value for every selected row, so that
        # e.g. 全国 and 東京都区部 are compared at the same date.
        full = max(filled.values()) if filled else 0
        complete = [t for t in with_data if filled.get(t) == full]
        chosen = set(complete[:n])
        if complete and with_data and complete[0] != with_data[0]:
            names = {it["code"]: it["name"] for it in by_id["time"]["items"]}
            skipped = [names.get(t, t) for t in with_data if t > complete[0]]
            extra_warnings.append(
                f"{'、'.join(skipped)} has values for only some of the selected rows, so the latest period "
                f"covering all of them ({names.get(complete[0])}) is shown. Ask for time='{skipped[0]}' "
                "to see the newer figures that exist.")
        with_data = complete
        rows_raw = [r for r in rows_raw if r.get("@time") in chosen]
        label = _label(by_id["time"])
        if label in defaults and with_data:
            name = next((it["name"] for it in by_id["time"]["items"] if it["code"] == with_data[0]), with_data[0])
            defaults[label] = f"latest period with data ({name})"

    if not rows_raw:
        return _dump({
            "table": header,
            "message": "The table has no values for this selection.",
            "selection": _selection_text(dims, selections),
            "try_next": ["Check the period: use time='latest' or look at the time range with estat_table_info.",
                         "Some combinations are not tabulated (e.g. small areas × detailed industries)."],
        })
    if len(rows_raw) > max_rows:
        return _dump(_needs_narrowing(table_id, header, dims, selections, len(rows_raw), max_rows, defaults))

    extra_warnings += _cpi_preliminary_warning(table, by_id, rows_raw)
    extra_warnings += _missing_items_warning(by_id, selections, explicit, rows_raw)
    time_pinned = "time" in explicit and not (selections.get("time") and selections["time"].latest)
    if not time_pinned:
        extra_warnings += _stale_warning(table, by_id, rows_raw)
    return _dump(_shape(table, header, dims, rows_raw, notes_def, defaults, extra_warnings))


def _missing_items_warning(by_id, selections, explicit, rows_raw) -> list[str]:
    """Items the caller asked for by name that have no value in the rows returned."""
    out = []
    for did in explicit:
        sel = selections.get(did)
        if not sel or sel.codes is None or (did == "time" and sel.latest):
            continue
        present = {r.get(f"@{did}") for r in rows_raw if _number(str(r.get("$", ""))) is not None}
        missing = [c for c in sel.codes if c not in present]
        if missing:
            names = {it["code"]: it["name"] for it in by_id[did]["items"]}
            listed = "、".join(f"{names.get(c, c)} (code {c})" for c in missing[:10])
            when = ""
            if did != "time" and "time" in by_id:
                periods = sorted({r.get("@time") for r in rows_raw})
                tnames = {it["code"]: it["name"] for it in by_id["time"]["items"]}
                when = " for " + "、".join(tnames.get(t, t) for t in periods[:5])
            out.append(f"No values{when} for: {listed}. Those items are left out of the table; e-Stat has "
                       "no figure for them in this selection (try another period with time='all').")
    return out


def _stale_warning(table: dict, by_id: dict, rows_raw: list) -> list[str]:
    """The newest period in the e-Stat database can lag years behind the latest published results."""
    if "time" not in by_id or not rows_raw:
        return []
    names = {it["code"]: it["name"] for it in by_id["time"]["items"]}
    newest_overall = max((it["code"] for it in by_id["time"]["items"]), default="")
    latest = max(r.get("@time", "") for r in rows_raw)
    if latest != newest_overall:
        return []  # the caller picked an older period on purpose, or newer ones exist in the table
    name = names.get(latest, "")
    m = re.match(r"(\d{4})", unicodedata.normalize("NFKC", name))
    if not m:
        return []
    year = int(m.group(1))
    gran = resolve.time_granularity(name)
    now = time.localtime()
    age = now.tm_year - year
    if gran in ("year", "fiscal_year", "other") and age < 2:
        return []
    if gran in ("month", "quarter") and (now.tm_year * 12 + now.tm_mon) - (year * 12 + _month(name)) < 6:
        return []
    code = (table.get("STAT_NAME") or {}).get("@code") if isinstance(table.get("STAT_NAME"), dict) else None
    where = f" (https://www.e-stat.go.jp/stat-search/files?toukei={code})" if code else ""
    return [f"The newest period in this e-Stat database table is {name}. Newer results of this statistic may "
            f"be published only as files (Excel/PDF) on e-Stat{where}; check there before calling {name} "
            "the latest figure."]


def _month(name: str) -> int:
    m = re.search(r"(\d{1,2})月", unicodedata.normalize("NFKC", name))
    return int(m.group(1)) if m else 12


def _cpi_preliminary_warning(table: dict, by_id: dict, rows_raw: list) -> list[str]:
    """Tokyo-ku CPI for a month that has no national figure yet is the mid-month estimate (中旬速報値)."""
    if "消費者物価指数" not in _statistic_name(table) or "area" not in by_id or "time" not in by_id:
        return []
    areas = {it["code"]: it["name"] for it in by_id["area"]["items"]}
    tokyo = {r.get("@time") for r in rows_raw if "東京都区部" in areas.get(r.get("@area"), "")}
    national = next((c for c, name in areas.items() if resolve.bare(name) == "全国"), None)
    if not tokyo or not national:
        return []
    probe = client.call("getStatsData", {
        "statsDataId": table.get("@id"), "cdArea": national, "cdTime": ",".join(sorted(tokyo)),
        "metaGetFlg": "N", "explanationGetFlg": "N", "annotationGetFlg": "N", "limit": 1000,
    })
    have = {v.get("@time") for v in _as_list(probe.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE"))
            if _number(str(v.get("$", ""))) is not None}
    early = sorted(tokyo - have)
    if not early:
        return []
    names = {it["code"]: it["name"] for it in by_id["time"]["items"]}
    months = "、".join(names.get(c, c) for c in early)
    return [f"東京都区部 for {months} is the mid-month preliminary estimate (中旬速報値), published before the "
            "national figure; the final value comes out with the national release the following month."]


def _seasonal_hint(dims, rows_raw) -> str:
    """Point at the seasonally adjusted item press releases quote for 前月比, if the table has one."""
    text = ("前月比 here is the raw month-on-month change, not seasonally adjusted. Press releases quote the "
            "seasonally adjusted 前月比, which can differ")
    for d in dims:
        if not d["id"].startswith("cat"):
            continue
        by_name = {resolve.bare(it["name"]): it for it in d["items"]}
        used = {r.get(f"@{d['id']}") for r in rows_raw}
        for it in d["items"]:
            if it["code"] in used:
                adjusted = by_name.get(resolve.bare(it["name"]) + resolve.bare("（季節調整済）"))
                if adjusted:
                    return (text + f": ask for {d['name']}='{adjusted['name']}' (code {adjusted['code']}) "
                            "to get that figure.")
    return text + "."


def _selection_text(dims, selections) -> dict:
    out = {}
    for d in dims:
        s = selections.get(d["id"])
        by_code = {it["code"]: it["name"] for it in d["items"]}
        if s and s.codes is not None:
            out[_label(d)] = [by_code.get(c, c) for c in s.codes[:10]] + (["…"] if len(s.codes) > 10 else [])
        else:
            out[_label(d)] = f"all {len(d['items'])}"
    return out


def _needs_narrowing(table_id, header, dims, selections, count, max_rows, defaults) -> dict:
    sizes = []
    for d in dims:
        s = selections.get(d["id"])
        n = len(s.codes) if s and s.codes is not None else len(d["items"])
        if s and s.latest:
            n = s.latest[0]
        if n > 1:
            sizes.append({"dimension": _label(d), "key": d["id"], "selected_items": n,
                          "examples": [it["name"] for it in _ordered(d)[:4]]})
    sizes.sort(key=lambda x: -x["selected_items"])
    return {
        "error": f"This selection has about {count} values, more than max_rows={max_rows}.",
        "kind": "needs_narrowing",
        "table": {"id": table_id, "title": header.get("title")},
        "widest_dimensions": sizes,
        "how_to_narrow": [
            "Pick specific items in the widest dimension, e.g. area='東京都' or a filters entry.",
            "For many areas use area='children:東京都' (sub-areas of one place) or area='level:2'.",
            "For a time series use time='latest:12' or a range such as '2024年1月~2026年8月'.",
            f"Or raise max_rows (up to {HARD_MAX_ROWS}).",
        ],
        **({"defaults_applied": defaults} if defaults else {}),
    }


def _shape(table, header, dims, rows_raw, notes_def, defaults, extra_warnings=()) -> dict:
    by_dim = {d["id"]: {it["code"]: it for it in d["items"]} for d in dims}
    base = _base_year(_statistic_name(table))
    present = [d for d in dims if any(f"@{d['id']}" in r for r in rows_raw[:1])]
    varying = [d for d in present if len({r.get(f"@{d['id']}") for r in rows_raw}) > 1]
    duplicated = {}
    for d in dims:
        seen, dup = set(), set()
        for it in d["items"]:
            (dup if it["name"] in seen else seen).add(it["name"])
        duplicated[d["id"]] = dup

    def name_of(d, code):
        it = by_dim[d["id"]].get(code)
        if not it:
            return code
        # Same-named items (e.g. two '所定内給与額' series) keep their code so they stay apart.
        if it["name"] not in duplicated[d["id"]]:
            return it["name"]
        parent = by_dim[d["id"]].get(it.get("parent") or "")
        if d["id"] == "area" and parent:
            return f"{parent['name']} {it['name']}"
        return f"{it['name']} ({code})"

    # e-Stat files month-on-month, year-on-year and fiscal-year changes under one combined name;
    # say which one each row is. Monthly CPI changes here are raw, not seasonally adjusted.
    time_dim = next((d for d in dims if d["id"] == "time"), {"items": []})
    time_names = {it["code"]: it["name"] for it in time_dim["items"]}
    change_words = {"month": "前月比", "quarter": "前期比", "year": "前年比", "fiscal_year": "前年度比"}
    raw_monthly_change = []

    cat_ids = [d["id"] for d in dims if d["id"].startswith("cat")]

    def adjusted(r):
        return any("季節調整" in by_dim[c].get(r.get(f"@{c}"), {}).get("name", "") for c in cat_ids)

    def change_label(name, rows):
        if resolve.bare(name) != resolve.bare("前月比・前年比・前年度比"):
            return name
        grans = {resolve.time_granularity(time_names.get(r.get("@time"), "")) for r in rows}
        if len(grans) != 1:
            return name
        word = change_words.get(grans.pop(), name)
        if word == "前月比":
            if all(adjusted(r) for r in rows):
                return "前月比（季節調整済）"
            raw_monthly_change.append(True)
            return "前月比（原数値・季節調整なし）"
        return word

    fixed = {}
    for d in present:
        if d not in varying:
            label = name_of(d, rows_raw[0].get(f"@{d['id']}"))
            if d["id"] == "tab":
                label = change_label(label, rows_raw)
            fixed[_label(d)] = label

    def unit_of(r):
        if r.get("@unit"):
            return r["@unit"]
        for did in ("tab", *[d["id"] for d in dims if d["id"].startswith("cat")]):
            it = by_dim.get(did, {}).get(r.get(f"@{did}"))
            if it and it.get("unit"):
                return it["unit"]
        tab = by_dim.get("tab", {}).get(r.get("@tab"))
        if base and tab and "指数" in tab["name"]:
            return f"指数（{base}年=100）"
        return ""

    if "time" in [d["id"] for d in varying]:
        rows_raw = sorted(rows_raw, key=lambda r: r.get("@time", ""))
    units = [unit_of(r) for r in rows_raw]
    same_unit = len(set(units)) == 1
    columns = [_label(d) for d in varying] + ["value"] + ([] if same_unit else ["unit"])
    rows, used_chars, annotations = [], set(), {}
    for r, unit in zip(rows_raw, units):
        text = str(r.get("$", ""))
        num = _number(text)
        if num is None:
            used_chars.add(text)
        row = [change_label(name_of(d, r.get(f"@{d['id']}")), [r]) if d["id"] == "tab"
               else name_of(d, r.get(f"@{d['id']}")) for d in varying]
        row.append(num if num is not None else text)
        if not same_unit:
            row.append(unit)
        if r.get("@anno"):
            annotations[r["@anno"]] = notes_def.get(r["@anno"], "")
        rows.append(row)

    result = {"table": header}
    if fixed:
        result["fixed"] = fixed
    if same_unit and units[0]:
        result["unit"] = units[0]
    result["columns"] = columns
    result["rows"] = rows
    result["row_count"] = len(rows)
    if defaults:
        result["defaults_applied"] = defaults
    symbols = {c: notes_def.get(c, "") for c in sorted(used_chars)}
    notes = []
    if symbols:
        notes.append({"symbols": symbols})
    if annotations:
        notes.append({"annotations": annotations})
    if notes:
        result["notes"] = notes
    warnings = _table_flags(table) + list(extra_warnings)
    if raw_monthly_change:
        warnings.append(_seasonal_hint(dims, rows_raw))
    newer = _newer_base_warning(table)
    if newer:
        warnings.append(newer)
    if warnings:
        result["warnings"] = warnings
    result["source"] = _source(header)
    return result


HANDLERS = {
    "estat_search_tables": search_tables,
    "estat_table_info": table_info,
    "estat_get_data": get_data,
}

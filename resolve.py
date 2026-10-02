"""Turn the names people use ("東京都", "2020年", "令和2年", "latest") into e-Stat codes.

e-Stat tables identify every row by codes ("13000", "2020000000", "0001").
Item names differ from table to table ("総合" vs "0001 総合", "東京都" vs
"13100 東京都区部"), and the same municipality name can exist twice ("府中市"
in Tokyo and Hiroshima). This module does the matching and, when a name does
not match exactly one item, says which items come closest.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

_CODE_PREFIX = re.compile(r"^[0-9A-Za-z_]+(?:[～~\-][0-9A-Za-z_]+)?[\s　]+(?=\S)")
_SPACES = re.compile(r"[\s　]+")

WAREKI = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911}
WAREKI_LETTER = {"R": 2018, "H": 1988, "S": 1925}

DIMENSION_ALIASES = {
    "area": {"area", "region", "地域", "場所", "都道府県", "市区町村", "prefecture", "municipality", "place"},
    "time": {"time", "year", "period", "month", "date", "時間", "時間軸", "時点", "年", "年次", "年月", "期間"},
    "tab": {"tab", "measure", "indicator", "表章事項", "表章項目", "表章"},
}

ALL_VALUES = {"all", "*", "すべて", "全て", "全部"}
TOTAL_NAMES = {
    "総数", "総計", "合計", "計", "総合", "全体", "全産業", "産業計", "男女計", "全規模", "年齢計",
    "学歴計", "全世帯", "総世帯", "全国計", "全年齢", "企業規模計",
}
TOTAL_PREFIXES = ("総数", "総計", "合計", "全産業", "産業計")


def norm(text) -> str:
    """NFKC, no spaces, lower-case. '０００１　総合' -> '0001総合'."""
    text = unicodedata.normalize("NFKC", str(text))
    return _SPACES.sub("", text).lower()


def bare(name) -> str:
    """Drop a leading code ('0001 総合' -> '総合', 'D 建設業' -> '建設業') and normalise."""
    text = unicodedata.normalize("NFKC", str(name)).strip()
    return norm(_CODE_PREFIX.sub("", text))


def split_values(value) -> list[str]:
    """A filter value may be a list, or one string with several values separated by ','."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(split_values(v))
        return out
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,、]", text) if part.strip()]


# --- dimensions ---------------------------------------------------------------


def find_dimension(key: str, dims: list[dict]) -> dict | None:
    """Match a filter key to a dimension by id ('area', 'cat01'), alias or name ('男女')."""
    k = norm(key)
    for d in dims:
        if norm(d["id"]) == k:
            return d
    for dim_id, aliases in DIMENSION_ALIASES.items():
        if k in {norm(a) for a in aliases}:
            for d in dims:
                if d["id"] == dim_id:
                    return d
    for d in dims:
        if bare(d["name"]) == bare(key):
            return d
    candidates = [d for d in dims if k and k in norm(d["name"])]
    if len(candidates) == 1:
        return candidates[0]
    return None


# --- time ---------------------------------------------------------------------


def time_granularity(name: str) -> str:
    n = norm(name)
    if "年度" in n:
        return "fiscal_year"
    if re.search(r"\d+月(?!.*期)", n) and not re.search(r"[～~-]", n):
        return "month"
    if "期" in n or "四半期" in n or re.search(r"\d+月[～~-]\d+月", n):
        return "quarter"
    if re.search(r"\d{4}年$", n) or re.search(r"\d{4}年\(", n):
        return "year"
    return "other"


GRANULARITY_ORDER = ["month", "quarter", "year", "fiscal_year", "other"]
_GRANULARITY_WORDS = {
    "month": "month", "monthly": "month", "月": "month", "月次": "month",
    "quarter": "quarter", "四半期": "quarter", "期": "quarter",
    "year": "year", "annual": "year", "年": "year", "年次": "year",
    "fiscalyear": "fiscal_year", "fy": "fiscal_year", "年度": "fiscal_year",
}

_LATEST = re.compile(
    r"^(?:latest|last|newest|recent|最新|直近)\s*(?P<gran>[a-z年月度次期四半]*)?\s*[:：]?\s*(?P<n>\d+)?\s*(?P<gran2>[a-z年月度次期四半]*)?$"
)


def parse_latest(value: str):
    """'latest' -> (1, None); 'latest:12' -> (12, None); 'latest month:12' -> (12, 'month')."""
    text = norm(value).replace("_", "")
    m = _LATEST.match(text)
    if not m:
        return None
    n = int(m.group("n") or 1)
    gran_word = (m.group("gran") or m.group("gran2") or "").strip()
    gran = _GRANULARITY_WORDS.get(gran_word) if gran_word else None
    if gran_word and gran is None:
        return None
    return max(1, min(n, 120)), gran


def time_targets(value: str) -> list[str]:
    """Normalised item names a time value could mean. '2026-08' -> ['2026年8月']; '令和2年' -> ['2020年']."""
    text = norm(value)
    out = []
    for era, offset in WAREKI.items():
        m = re.match(rf"^{era}(\d+|元)年(.*)$", text)
        if m:
            year = offset + (1 if m.group(1) == "元" else int(m.group(1)))
            text = f"{year}年{m.group(2)}"
    m = re.match(r"^([rhs])(\d{1,2})$", text)
    if m:
        text = f"{WAREKI_LETTER[m.group(1).upper()] + int(m.group(2))}年"
    m = re.match(r"^(?:fy)(\d{4})$", text)
    if m:
        text = f"{m.group(1)}年度"
    m = re.match(r"^(\d{4})[-/.](\d{1,2})$", text) or re.match(r"^(\d{4})(\d{2})$", text)
    if m and 1 <= int(m.group(2)) <= 12:
        text = f"{int(m.group(1))}年{int(m.group(2))}月"
    m = re.match(r"^(\d{4})年(\d{1,2})月$", text)
    if m:
        text = f"{int(m.group(1))}年{int(m.group(2))}月"
    if re.match(r"^\d{4}$", text):
        text += "年"
    out.append(text)
    return out


# --- item matching ------------------------------------------------------------


class Ambiguous(Exception):
    def __init__(self, value, candidates):
        super().__init__(value)
        self.value = value
        self.candidates = candidates


class NoMatch(Exception):
    def __init__(self, value, suggestions):
        super().__init__(value)
        self.value = value
        self.suggestions = suggestions


def _descendants(items: list[dict], parent_code: str) -> list[dict]:
    children = {}
    for it in items:
        children.setdefault(it.get("parent"), []).append(it)
    out, stack = [], [parent_code]
    while stack:
        code = stack.pop()
        for child in children.get(code, []):
            out.append(child)
            stack.append(child["code"])
    return out


def _pick(value: str, matches: list[dict]) -> dict:
    if len(matches) == 1:
        return matches[0]
    raise Ambiguous(value, matches)


def match_item(value: str, items: list[dict], dim_id: str) -> dict:
    """Find the one item a value names. Raises Ambiguous or NoMatch."""
    raw = str(value).strip()
    for it in items:
        if it["code"] == raw:
            return it

    targets = {norm(raw), bare(raw)}
    if dim_id == "time":
        targets.update(time_targets(raw))

    exact = [it for it in items if bare(it["name"]) in targets or norm(it["name"]) in targets]
    if exact:
        return _pick(raw, exact)

    key = bare(raw)
    if dim_id == "area":
        variants = [key + suffix for suffix in ("都", "府", "県", "市", "区", "町", "村")]
        hits = [it for it in items if bare(it["name"]) in variants]
        if hits:
            return _pick(raw, hits)
        # "東京都府中市", "広島県 府中市": a parent name followed by a child name.
        for parent in items:
            pname = bare(parent["name"])
            if pname and key.startswith(pname) and len(key) > len(pname):
                rest = key[len(pname):]
                under = _descendants(items, parent["code"])
                hits = [it for it in under if bare(it["name"]) in (rest, *[rest + s for s in "都府県市区町村"])]
                if hits:
                    return _pick(raw, hits)

    if dim_id == "time":
        starts = [it for it in items if any(norm(it["name"]).startswith(t) for t in targets)]
        if starts:
            return _pick(raw, starts)

    contains = [it for it in items if key and key in bare(it["name"])]
    if len(contains) == 1:
        return contains[0]
    if contains:
        exactish = [it for it in contains if bare(it["name"]).startswith(key)]
        if len(exactish) == 1 and len(contains) <= 3:
            return exactish[0]
        raise Ambiguous(raw, contains)

    names = {bare(it["name"]): it for it in items}
    close = difflib.get_close_matches(key, list(names), n=6, cutoff=0.5)
    suggestions = [names[c] for c in close]
    if not suggestions and key:
        chars = set(key)
        scored = sorted(items, key=lambda it: -len(chars & set(bare(it["name"]))))
        suggestions = [it for it in scored[:6] if len(chars & set(bare(it["name"]))) >= min(2, len(chars))]
    raise NoMatch(raw, suggestions)


def total_item(items: list[dict]) -> dict | None:
    """The item that stands for 'everything' in a classification (総数, 総合, 全産業 ...), if any."""
    if len(items) <= 1:
        return items[0] if items else None
    hits = [
        it for it in items
        if bare(it["name"]) in TOTAL_NAMES or bare(it["name"]).startswith(TOTAL_PREFIXES)
        or (bare(it["name"]).endswith("計") and len(bare(it["name"])) <= 8)
        or re.match(r"^[^()]{1,8}計\(", bare(it["name"]))
    ]
    if not hits:
        return None
    levels = [int(it["level"]) if str(it.get("level", "")).isdigit() else 99 for it in hits]
    return hits[levels.index(min(levels))]


def describe(item: dict, by_code: dict | None = None) -> str:
    """'府中市 (code 34208, under 広島県)' — enough to tell two same-named items apart."""
    text = f"{item['name']} (code {item['code']}"
    parent = item.get("parent")
    if parent and by_code and parent in by_code:
        text += f", under {by_code[parent]['name']}"
    if item.get("unit"):
        text += f", unit {item['unit']}"
    return text + ")"

import pytest


@pytest.fixture
def r(plugin):
    return plugin.resolve


def test_bare_drops_leading_codes_and_normalises(r):
    assert r.bare("0001 総合") == "総合"
    assert r.bare("13100 東京都区部") == "東京都区部"
    assert r.bare("A～R 全産業（S公務を除く）") == "全産業(s公務を除く)"
    assert r.bare("２０２０年") == "2020年"


@pytest.mark.parametrize("value, expected", [
    ("2020", "2020年"), ("2020年", "2020年"), ("令和2年", "2020年"), ("令和元年", "2019年"),
    ("平成27年", "2015年"), ("R2", "2020年"), ("2026-08", "2026年8月"), ("2026/8", "2026年8月"),
    ("202608", "2026年8月"), ("2026年08月", "2026年8月"), ("FY2025", "2025年度"),
])
def test_time_targets(r, value, expected):
    assert r.time_targets(value)[0] == expected


@pytest.mark.parametrize("value, expected", [
    ("latest", (1, None)), ("latest:12", (12, None)), ("最新", (1, None)), ("直近12", (12, None)),
    ("latest month:6", (6, "month")), ("latest year", (1, "year")), ("2020年", None),
])
def test_parse_latest(r, value, expected):
    assert r.parse_latest(value) == expected


def test_time_granularity(r):
    assert r.time_granularity("2026年8月") == "month"
    assert r.time_granularity("2025年度") == "fiscal_year"
    assert r.time_granularity("2025年") == "year"
    assert r.time_granularity("2025年1～3月期") == "quarter"


def test_total_item_finds_the_overall_category(r):
    items = [{"code": c, "name": n, "level": lv} for c, n, lv in [
        ("AR", "全産業（S_公務を除く）", "1"), ("D", "建設業", "2"), ("E", "製造業", "2")]]
    assert r.total_item(items)["code"] == "AR"
    sexes = [{"code": "0", "name": "総数", "level": "1"}, {"code": "1", "name": "男", "level": "1"}]
    assert r.total_item(sexes)["code"] == "0"
    assert r.total_item([{"code": "1", "name": "男", "level": "1"}, {"code": "2", "name": "女", "level": "1"}]) is None


def test_split_values(r):
    assert r.split_values("東京都,大阪府") == ["東京都", "大阪府"]
    assert r.split_values("東京都、大阪府") == ["東京都", "大阪府"]
    assert r.split_values(["東京都", "大阪府,京都府"]) == ["東京都", "大阪府", "京都府"]


def test_total_item_with_a_qualifier_and_full_width_codes(r):
    sizes = [{"code": "01", "name": "企業規模計（10人以上）", "level": ""},
             {"code": "02", "name": "1,000人以上", "level": ""}]
    assert r.total_item(sizes)["code"] == "01"
    industries = [{"code": "01", "name": "Ｔ１ 産業計", "level": ""}, {"code": "03", "name": "Ｄ 建設業", "level": ""}]
    assert r.total_item(industries)["code"] == "01"
    assert r.match_item("建設業", industries, "cat04")["code"] == "03"

import json
import urllib.error
from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TOOL_NAMES = ["estat_search_tables", "estat_table_info", "estat_get_data"]


# --- registration and manifest --------------------------------------------------------


def test_register_wires_three_tools_to_the_estat_toolset(plugin):
    registered = {}

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kwargs):
            registered[name] = (toolset, schema, handler, kwargs)

    plugin.register(Ctx())
    assert list(registered) == TOOL_NAMES
    for name, (toolset, schema, handler, kwargs) in registered.items():
        assert toolset == "estat"
        assert schema["name"] == name
        assert callable(handler)
        assert kwargs["requires_env"] == ["ESTAT_APP_ID"]


def test_manifest_declares_exactly_what_is_registered(plugin):
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    registered = {}

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kwargs):
            registered[name] = kwargs["requires_env"]

    plugin.register(Ctx())
    assert manifest["name"] == "jp-estat"
    assert manifest["provides_tools"] == list(registered)
    assert "provides_hooks" not in manifest and "provides_middleware" not in manifest
    assert [e["name"] for e in manifest["requires_env"]] == ["ESTAT_APP_ID"]
    assert all(env == ["ESTAT_APP_ID"] for env in registered.values())
    assert manifest["requires_hermes"] == ">=0.18"
    assert plugin.client.USER_AGENT.endswith("/" + manifest["version"])


def test_schemas_are_valid_function_definitions(plugin):
    for schema in plugin.schemas.ALL_SCHEMAS:
        params = schema["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])
        assert len(schema["description"]) > 200


# --- estat_get_data: numbers that must match the published figures --------------------


def test_census_2020_tokyo_and_japan_total_population(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "東京都,全国", "time": "2020年"})
    rows = dict(out["rows"])
    assert rows == {"東京都": 14047594, "全国": 126146099}
    assert out["unit"] == "人"
    assert out["fixed"]["男女"] == "総数"
    assert out["defaults_applied"] == {"男女": "総数"}
    assert out["table"]["survey_date"] == "2020-10"
    assert out["source"]["citation"].startswith("出典：政府統計の総合窓口(e-Stat)（https://www.e-stat.go.jp/）")
    assert "（総務省）" in out["source"]["citation"]
    assert "国によって保証されたものではありません" in out["source"]["credit"]
    assert out["source"]["table_url"] == "https://www.e-stat.go.jp/dbview?sid=0003445078"


def test_wareki_and_codes_work_as_well_as_names(run):
    a = run("estat_get_data", {"table_id": "0003445078", "area": "13000", "time": "令和2年"})
    b = run("estat_get_data", {"table_id": "0003445078", "area": "東京", "time": "2020"})
    assert a["rows"] == b["rows"] == [[14047594]]


def test_cpi_defaults_to_latest_national_all_items(run):
    out = run("estat_get_data", {"table_id": "0004052037"})
    assert out["fixed"] == {"2025年基準品目": "0001 総合", "地域": "全国", "時点": "2026年8月"}
    assert out["rows"] == [["指数", 102.2, "指数（2025年=100）"],
                           ["前月比（原数値・季節調整なし）", 0.1, "%"],
                           ["前年同月比", 1.9, "%"]]
    assert out["defaults_applied"]["時点"] == "latest period with data (2026年8月)"
    # The press release quotes the seasonally adjusted month-on-month change; say so.
    assert any(w.startswith("前月比 here is the raw month-on-month change") for w in out["warnings"])


def test_cpi_tokyo_newest_month_is_flagged_as_mid_month_preliminary(run):
    out = run("estat_get_data", {"table_id": "0004052037", "area": "東京都区部", "filters": {"tab": "指数"}})
    assert out["fixed"]["時点"] == "2026年9月"
    assert out["rows"] == [[102.5]]
    assert any("中旬速報値" in w for w in out["warnings"])


def test_latest_compares_areas_at_the_same_date(run):
    out = run("estat_get_data", {"table_id": "0004052037", "area": "東京都区部,全国", "filters": {"tab": "指数"}})
    assert out["fixed"]["時点"] == "2026年8月"
    assert dict(out["rows"]) == {"13100 東京都区部": 102.3, "全国": 102.2}
    assert any("2026年9月" in w for w in out["warnings"])


def test_latest_n_returns_a_chronological_series(run):
    out = run("estat_get_data", {"table_id": "0004052037", "time": "latest:3", "filters": {"表章項目": "前年同月比"}})
    assert out["columns"] == ["時点", "value"]
    assert [r[0] for r in out["rows"]] == ["2026年6月", "2026年7月", "2026年8月"]
    assert out["unit"] == "%"


def test_time_range_and_item_by_bare_name(run):
    out = run("estat_get_data", {"table_id": "0004052037", "time": "2026-01~2026-03",
                                 "filters": {"品目": "生鮮食品を除く総合", "tab": "指数"}})
    assert out["fixed"]["2025年基準品目"] == "0161 生鮮食品を除く総合"
    assert [r[0] for r in out["rows"]] == ["2026年1月", "2026年2月", "2026年3月"]


def test_latest_year_picks_calendar_year(run):
    out = run("estat_get_data", {"table_id": "0004052037", "time": "latest year", "filters": {"tab": "指数"}})
    assert out["fixed"]["時点"] == "2025年"
    assert out["rows"] == [[100]]


def test_children_lists_sub_areas(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "children:東京都"})
    names = [r[0] for r in out["rows"]]
    # Names that exist twice in the table carry their parent: "東京都 府中市".
    assert "特別区部" in names and "東京都 府中市" in names and "八王子市" in names
    assert "千代田区" not in names  # under 特別区部, one level further down


# --- estat_get_data: what must fail, and how ---------------------------------------


def test_same_name_in_two_prefectures_asks_for_a_choice(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "府中市"})
    assert out["kind"] == "needs_choice"
    issue = out["issues"][0]
    assert issue["candidates"] == ["府中市 (13206, 東京都)", "府中市 (34208, 広島県)"]
    assert "東京都府中市" in issue["problem"]


def test_prefecture_prefix_resolves_the_ambiguity(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "広島県府中市"})
    assert out["fixed"]["地域"] == "広島県 府中市"
    assert out["rows"] == [[37655]]


def test_unknown_area_suggests_close_names(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "東京府"})
    issue = out["issues"][0]
    assert out["kind"] == "needs_choice"
    assert issue["closest"][0] == "東京都 (13000)"
    assert "estat_table_info" in issue["see_all"]


def test_unknown_dimension_lists_the_real_ones(run):
    out = run("estat_get_data", {"table_id": "0003445078", "filters": {"年齢": "20歳"}})
    assert "no dimension called '年齢'" in out["error"]
    assert {d["key"] for d in out["dimensions"]} == {"tab", "cat01", "area", "time"}
    assert "estat_search_tables" in out["next_step"]


def test_too_many_rows_returns_advice_not_a_dump(run):
    out = run("estat_get_data", {"table_id": "0003445078", "area": "all", "max_rows": 5})
    assert out["kind"] == "needs_narrowing"
    assert out["widest_dimensions"][0]["key"] == "area"
    assert any("children:" in tip for tip in out["how_to_narrow"])


def test_unknown_table_id(run):
    out = run("estat_get_data", {"table_id": "0000000001"})
    assert out["kind"] == "not_found"
    assert "estat_search_tables" in out["error"]


# --- estat_search_tables ---------------------------------------------------------


def test_search_folds_repeats_and_ranks_the_main_table_first(run, api):
    out = run("estat_search_tables", {"query": "男女別人口", "survey": "国勢調査", "year": "2020"})
    ids = [t["id"] for t in out["tables"]]
    assert len(ids) == len(set(ids))
    assert ids[0] == "0003445078"
    assert out["tables"][0]["areas"].startswith("全国，都道府県，市区町村")
    params = [p for e, p in api.calls if e == "getStatsList" and "statsNameList" not in p][0]
    assert params["searchWord"] == "男女別人口" and params["statsCode"] == "00200521"
    assert params["surveyYears"] == "2020"


def test_search_flags_preliminary_tables(run):
    out = run("estat_search_tables", {"query": "男女別人口", "limit": 30})
    flagged = [t for t in out["tables"] if "速報" in t["statistic"]]
    assert flagged and all(any("preliminary" in n for n in t["notes"]) for t in flagged)


def test_search_marks_the_older_cpi_base_as_superseded(run):
    out = run("estat_search_tables", {"query": "消費者物価指数"})
    first = out["tables"][0]
    assert first["id"] == "0004052037" and "notes" not in first
    old = next(t for t in out["tables"] if t["id"] == "0003427113")
    assert any("2025年基準" in n for n in old["notes"])


def test_search_with_no_hits_explains_what_to_try(run):
    out = run("estat_search_tables", {"query": "ぴよぴよ統計"})
    assert out["total_matches"] == 0 and out["tables"] == []
    assert out["try_next"] and out["popular_sources"]


def test_search_no_hits_suggests_official_terms_and_dropping_filters(run):
    out = run("estat_search_tables", {"query": "給料 業種", "year": "2023"})
    tips = " ".join(out["try_next"])
    assert "'給料' -> '賃金'" in tips
    assert "19 tables match" in tips


def test_search_needs_a_query(run):
    out = run("estat_search_tables", {"query": "  "})
    assert "query is empty" in out["error"]


# --- estat_table_info -------------------------------------------------------------


def test_table_info_lists_dimensions_and_a_ready_call(run):
    out = run("estat_table_info", {"table_id": "0004052037"})
    keys = [d["key"] for d in out["dimensions"]]
    assert keys == ["tab", "cat01", "area", "time"]
    time_dim = out["dimensions"][3]
    assert time_dim["items"][0].startswith("2026年9月")
    assert out["example"] == {"table_id": "0004052037", "area": "13100 東京都区部", "time": "latest",
                              "filters": {"2025年基準品目": "0001 総合"}}


def test_table_info_search_within_a_dimension(run):
    out = run("estat_table_info", {"table_id": "0003445078", "dimension": "area", "search": "府中"})
    assert out["dimensions"][0]["items"] == ["府中市 (13206, 東京都)", "府中市 (34208, 広島県)"]


# --- the application ID never leaks ---------------------------------------------------


def test_missing_app_id_explains_how_to_get_one(plugin, monkeypatch):
    monkeypatch.delenv("ESTAT_APP_ID", raising=False)
    out = json.loads(plugin.tools.HANDLERS["estat_search_tables"]({"query": "人口"}))
    assert out["kind"] == "app_id_missing_or_invalid"
    assert "https://www.e-stat.go.jp/mypage/user/preregister" in out["error"]
    assert ".env file in your Hermes home" in out["error"]


def test_rejected_app_id_explains_how_to_fix_it(run, api):
    api.reject_app_id = True
    out = run("estat_search_tables", {"query": "人口"})
    assert out["kind"] == "app_id_missing_or_invalid"
    assert "rejected" in out["error"] and "appId=" not in out["error"]


def test_http_and_network_errors_never_carry_the_request_url(plugin, monkeypatch):
    secret = "SECRETAPPID999"
    monkeypatch.setenv("ESTAT_APP_ID", secret)
    monkeypatch.setattr(plugin.client, "RETRY_DELAY_SECONDS", 0)
    url = f"https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList?appId={secret}&searchWord=x"

    def http_error(request, timeout):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    def url_error(request, timeout):
        raise urllib.error.URLError(f"timed out while fetching {url}")

    for failure in (http_error, url_error):
        monkeypatch.setattr(plugin.client, "_open", failure)
        out = plugin.tools.HANDLERS["estat_search_tables"]({"query": "人口"})
        assert secret not in out and "appId=" not in out
        assert json.loads(out)["kind"] in ("http_error", "network_error")


def test_every_reply_in_this_suite_is_free_of_the_app_id(run, api):
    for tool, args in [
        ("estat_search_tables", {"query": "消費者物価指数"}),
        ("estat_table_info", {"table_id": "0003445078"}),
        ("estat_get_data", {"table_id": "0004052037", "time": "latest:12"}),
        ("estat_get_data", {"table_id": "0003445078", "area": "府中市"}),
    ]:
        run(tool, args)  # run() asserts the fake ID is absent from the raw reply
    assert api.app_ids == {"TESTAPPID0123456789abcdef"}


def test_redirect_to_another_host_is_refused(plugin):
    handler = plugin.client._SameHostRedirects()
    with pytest.raises(plugin.client.EstatError) as caught:
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/collect?appId=x")
    assert "refused" in str(caught.value) and "appId" not in str(caught.value)


def test_redirect_to_plain_http_on_the_same_host_is_refused(plugin):
    handler = plugin.client._SameHostRedirects()
    with pytest.raises(plugin.client.EstatError) as caught:
        handler.redirect_request(None, None, 301, "Moved", {},
                                 "http://api.e-stat.go.jp/rest/3.0/app/json/getStatsList?appId=x")
    assert "refused" in str(caught.value) and "appId" not in str(caught.value)


def test_one_tool_call_stops_at_its_time_budget(plugin, monkeypatch):
    monkeypatch.setenv("ESTAT_APP_ID", "SECRETAPPID999")
    calls = []

    def slow(request, timeout):
        calls.append(timeout)
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(plugin.client, "_open", slow)
    with plugin.client.budget(3):
        with pytest.raises(plugin.client.EstatError):
            plugin.client.call("getStatsList", {"searchWord": "人口"})
    assert len(calls) == 1 and calls[0] <= 3  # request capped by the budget, no retry without time left
    with plugin.client.budget(0.5):
        with pytest.raises(plugin.client.EstatError) as caught:
            plugin.client.call("getStatsList", {"searchWord": "人口"})
    assert caught.value.kind == "timeout" and len(calls) == 1


def test_explicit_period_gets_no_staleness_warning_but_latest_does(run):
    pinned = run("estat_get_data", {"table_id": "0003445078", "area": "東京都", "time": "2020年"})
    assert "warnings" not in pinned
    latest = run("estat_get_data", {"table_id": "0003445078", "area": "東京都"})
    assert any("stat-search/files?toukei=00200521" in w for w in latest["warnings"])


def test_requested_item_without_values_is_named(run):
    out = run("estat_get_data", {"table_id": "0004052037", "area": "全国,東京都区部", "time": "2026年9月",
                                 "filters": {"tab": "指数"}})
    assert [r[0] for r in out["rows"]] == ["13100 東京都区部"] or out["fixed"]["地域"] == "13100 東京都区部"
    assert any("No values" in w and "全国" in w for w in out["warnings"])


def test_failed_base_year_check_still_warns(plugin, api, monkeypatch):
    def boom(endpoint, params):
        raise plugin.client.EstatError("slow", kind="timeout")
    monkeypatch.setattr(plugin.client, "call", boom)
    table = {"STATISTICS_NAME": "2020年基準消費者物価指数", "STAT_NAME": {"@code": "00200573", "$": "消費者物価指数"}}
    assert "could not check" in plugin.tools._newer_base_warning(table)

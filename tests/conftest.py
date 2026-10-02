"""Load the repo root as a package (as Hermes does) and replay recorded e-Stat responses.

The fake API answers getStatsList / getMetaInfo / getStatsData from the JSON in
tests/fixtures, applying the same cd*/lv*/cntGetFlg/limit filtering e-Stat does,
so the tests run offline and need no application ID.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACKAGE = "estat_plugin"
FAKE_APP_ID = "TESTAPPID0123456789abcdef"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def plugin():
    return sys.modules.get(PACKAGE) or _load_plugin()


class FakeEstat:
    """Stands in for client._http_get(endpoint, params, app_id)."""

    tables = {
        "0003445078": ("meta_census.json", "values_census.json"),
        "0004052037": ("meta_cpi2025.json", "values_cpi2025.json"),
    }

    def __init__(self):
        self.calls = []
        self.app_ids = set()
        self.reject_app_id = False

    def __call__(self, endpoint, params, app_id):
        self.calls.append((endpoint, dict(params)))
        self.app_ids.add(app_id)
        if self.reject_app_id:
            return {"GET_STATS_LIST": {"RESULT": {"STATUS": 100, "ERROR_MSG": "認証に失敗しました。アプリケーションIDを確認して下さい。"}}}
        return getattr(self, endpoint)(params)

    @staticmethod
    def _status(root, status, message):
        return {root: {"RESULT": {"STATUS": status, "ERROR_MSG": message}}}

    def getStatsList(self, p):
        if p.get("statsNameList") == "Y":
            return fixture("names_census.json")
        if "ぴよ" in p.get("searchWord", ""):
            return {"GET_STATS_LIST": {"RESULT": {"STATUS": 1, "ERROR_MSG": "正常に終了しましたが、該当データはありませんでした。"},
                                       "DATALIST_INF": {"NUMBER": 0, "RESULT_INF": ""}}}
        if p.get("searchWord", "").startswith("給料") and p.get("surveyYears"):
            return {"GET_STATS_LIST": {"RESULT": {"STATUS": 1, "ERROR_MSG": "該当データなし"},
                                       "DATALIST_INF": {"NUMBER": 0}}}
        if p.get("searchWord", "").startswith("給料"):
            return {"GET_STATS_LIST": {"RESULT": {"STATUS": 0, "ERROR_MSG": ""}, "DATALIST_INF": {"NUMBER": 19}}}
        if p.get("statsCode") == "00200573" or "消費者物価指数" in p.get("searchWord", ""):
            return fixture("list_cpi.json")
        return fixture("list_census.json")

    def getMetaInfo(self, p):
        tid = p.get("statsDataId")
        if tid not in self.tables:
            return self._status("GET_META_INFO", 300, f"statsDataId=[{tid}]のデータは存在しません。IDを確認して下さい。")
        return fixture(self.tables[tid][0])

    def getStatsData(self, p):
        tid = p.get("statsDataId")
        if tid not in self.tables:
            return self._status("GET_STATS_DATA", 300, f"statsDataId=[{tid}]のデータは存在しません。")
        meta = fixture(self.tables[tid][0])["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
        levels = {o["@id"]: {c["@code"]: c.get("@level") for c in (o["CLASS"] if isinstance(o["CLASS"], list) else [o["CLASS"]])} for o in meta}
        data = fixture(self.tables[tid][1])
        values = data["VALUE"]
        for dim in ("tab", "time", "area", *[f"cat{i:02d}" for i in range(1, 16)]):
            name = {"tab": "Tab", "time": "Time", "area": "Area"}.get(dim, "Cat" + dim[3:])
            if f"cd{name}" in p:
                allowed = set(p[f"cd{name}"].split(","))
                values = [v for v in values if v.get(f"@{dim}") in allowed]
            if f"cd{name}From" in p:
                values = [v for v in values if v.get(f"@{dim}") >= p[f"cd{name}From"]]
            if f"cd{name}To" in p:
                values = [v for v in values if v.get(f"@{dim}") <= p[f"cd{name}To"]]
            if f"lv{name}" in p:
                values = [v for v in values if levels[dim].get(v.get(f"@{dim}")) == p[f"lv{name}"]]
        result = {"RESULT": {"STATUS": 0 if values else 1, "ERROR_MSG": "正常に終了しました。"},
                  "STATISTICAL_DATA": {"RESULT_INF": {"TOTAL_NUMBER": len(values)}}}
        if p.get("cntGetFlg") == "Y":
            return {"GET_STATS_DATA": result}
        limit = int(p.get("limit", 100000))
        result["STATISTICAL_DATA"]["DATA_INF"] = {"NOTE": data.get("NOTE"), "VALUE": values[:limit]}
        return {"GET_STATS_DATA": result}


@pytest.fixture
def api(plugin, monkeypatch):
    fake = FakeEstat()
    monkeypatch.setenv("ESTAT_APP_ID", FAKE_APP_ID)
    monkeypatch.setattr(plugin.client, "_http_get", fake)
    monkeypatch.setattr(plugin.client, "RETRY_DELAY_SECONDS", 0)
    plugin.client.clear_cache()
    plugin.tools._newer_base_cache.clear()
    return fake


@pytest.fixture
def run(plugin, api):
    """run(tool, args) -> parsed JSON reply, with the fake API in place."""
    def _run(tool, args):
        raw = plugin.tools.HANDLERS[tool](args)
        assert isinstance(raw, str)
        assert FAKE_APP_ID not in raw
        return json.loads(raw)
    return _run

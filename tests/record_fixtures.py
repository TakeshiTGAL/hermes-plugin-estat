"""Re-record the trimmed e-Stat responses the tests replay. Not collected by pytest.

    ESTAT_APP_ID=... python tests/record_fixtures.py

Fixtures are real API responses, cut down to a few areas, items and months so the
tests stay small. e-Stat does not echo the application ID, and this script scrubs
it anyway before writing.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures"
APP_ID = os.environ.get("ESTAT_APP_ID", "").strip()
BASE = "https://api.e-stat.go.jp/rest/3.0/app/json/"

CENSUS = "0003445078"
CPI_2025 = "0004052037"
CENSUS_AREAS = ["00000", "01000", "01100", "13000", "13100", "13101", "13201", "13206", "13229",
                "26000", "27000", "27100", "34000", "34208"]
CPI_ITEMS = ["0001", "0002", "0161"]
CPI_AREAS = ["00000", "13100"]


def get(endpoint, **params):
    query = urllib.parse.urlencode({"appId": APP_ID, **params})
    with urllib.request.urlopen(BASE + endpoint + "?" + query, timeout=60) as response:
        text = response.read().decode("utf-8")
    return json.loads(text.replace(APP_ID, "***"))


def trim_meta(meta, keep):
    for obj in meta["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]:
        rule = keep.get(obj["@id"])
        if rule is None:
            continue
        classes = obj["CLASS"] if isinstance(obj["CLASS"], list) else [obj["CLASS"]]
        obj["CLASS"] = [c for c in classes if rule(c)]
    return meta


def write(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote", name)


def main():
    if not APP_ID:
        sys.exit("Set ESTAT_APP_ID first.")
    OUT.mkdir(exist_ok=True)

    write("meta_census.json", trim_meta(
        get("getMetaInfo", statsDataId=CENSUS, explanationGetFlg="N"),
        {"area": lambda c: c["@code"] in CENSUS_AREAS}))
    data = get("getStatsData", statsDataId=CENSUS, cdArea=",".join(CENSUS_AREAS),
               metaGetFlg="N", explanationGetFlg="N")
    write("values_census.json", data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"])

    months = lambda c: c["@code"] >= "2025000909" or c["@code"] in ("2025000000", "2024000000")
    write("meta_cpi2025.json", trim_meta(
        get("getMetaInfo", statsDataId=CPI_2025, explanationGetFlg="N"),
        {"cat01": lambda c: c["@code"] in CPI_ITEMS, "area": lambda c: c["@code"] in CPI_AREAS, "time": months}))
    data = get("getStatsData", statsDataId=CPI_2025, cdCat01=",".join(CPI_ITEMS), cdArea=",".join(CPI_AREAS),
               cdTimeFrom="2025000909", metaGetFlg="N", explanationGetFlg="N")
    values = data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]
    values["VALUE"] += get("getStatsData", statsDataId=CPI_2025, cdCat01=",".join(CPI_ITEMS),
                           cdArea=",".join(CPI_AREAS), cdTime="2024000000,2025000000", metaGetFlg="N",
                           explanationGetFlg="N")["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
    write("values_cpi2025.json", values)

    lst = get("getStatsList", searchWord="男女別人口", statsCode="00200521", surveyYears="2020",
              limit=40, explanationGetFlg="N")
    write("list_census.json", lst)
    write("list_cpi.json", get("getStatsList", statsCode="00200573", limit=100, explanationGetFlg="N"))
    write("names_census.json", get("getStatsList", statsNameList="Y", searchWord="国勢調査"))


if __name__ == "__main__":
    main()

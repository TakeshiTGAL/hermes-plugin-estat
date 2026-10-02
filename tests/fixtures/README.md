# Recorded e-Stat responses (trimmed)

Responses of the e-Stat API v3 (`https://api.e-stat.go.jp/rest/3.0/app/json/`), all retrieved on 2026-10-02 with `tests/record_fixtures.py` for the offline tests. The data tables were cut down to a few areas, items and periods; the search results are unedited. They contain no personal data and no application ID.

出典：政府統計の総合窓口(e-Stat)（https://www.e-stat.go.jp/）「令和２年国勢調査 人口等基本集計」「2025年基準消費者物価指数」（総務省）を加工して作成（2026年10月2日に利用）

| File | Request | Source page (出典) | Kept (加工の内容) |
|---|---|---|---|
| `meta_census.json` | `getMetaInfo?statsDataId=0003445078` | https://www.e-stat.go.jp/dbview?sid=0003445078 （令和２年国勢調査 人口等基本集計 1-1-1 男女別人口，総務省） | 地域を14件（全国・北海道・札幌市・東京都・特別区部・千代田区・八王子市・府中市（東京都）・西東京市・京都府・大阪府・大阪市・広島県・府中市（広島県））に絞った。他の分類は無加工 |
| `values_census.json` | `getStatsData?statsDataId=0003445078&cdArea=（上の14地域）` | https://www.e-stat.go.jp/dbview?sid=0003445078 | `DATA_INF`（注記と値）のみ |
| `meta_cpi2025.json` | `getMetaInfo?statsDataId=0004052037` | https://www.e-stat.go.jp/dbview?sid=0004052037 （2025年基準消費者物価指数，総務省） | 品目を3件（総合・食料・生鮮食品を除く総合）、地域を2件（全国・東京都区部）、時点を2025年9月以降と2024年・2025年に絞った |
| `values_cpi2025.json` | `getStatsData?statsDataId=0004052037`（上の品目・地域、2025年9月以降と2024年・2025年） | https://www.e-stat.go.jp/dbview?sid=0004052037 | `DATA_INF`（注記と値）のみ |
| `list_census.json` | `getStatsList?searchWord=男女別人口&statsCode=00200521&surveyYears=2020&limit=40` | https://www.e-stat.go.jp/ （統計表の検索結果） | 無加工 |
| `list_cpi.json` | `getStatsList?statsCode=00200573&limit=100` | https://www.e-stat.go.jp/ （統計表の検索結果） | 無加工 |
| `names_census.json` | `getStatsList?statsNameList=Y&searchWord=国勢調査` | https://www.e-stat.go.jp/ （統計調査名の一覧） | 無加工 |

Source: e-Stat, the portal site for official statistics of Japan (https://www.e-stat.go.jp/); 2020 Population Census and 2025-base Consumer Price Index, Statistics Bureau, Ministry of Internal Affairs and Communications. Retrieved 2026-10-02 and trimmed as listed above.

このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。

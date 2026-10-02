# jp-estat: Japanese government statistics for Hermes Agent

Japan's official government statistics in [Hermes Agent](https://hermes-agent.nousresearch.com), built on the API of **e-Stat** (政府統計の総合窓口), the portal run by the Statistics Bureau of Japan and the National Statistics Center. Ask in plain words; the agent finds the right table, picks the right rows by name, and answers with the unit, the survey date and a citation you can paste into a report.

```
You:    What was Tokyo's population in the 2020 census, and Japan's?
Hermes: Tokyo 14,047,594 people, Japan 126,146,099 (2020 Census, as of 1 October 2020).
        出典：政府統計の総合窓口(e-Stat)「令和２年国勢調査 人口等基本集計 1-1-1 …」（総務省）
```

[日本語の説明は下にあります](#日本語)

## Install

1. Get a free e-Stat application ID ([how](#getting-an-application-id)).
2. Install and enable the plugin. Hermes asks for `ESTAT_APP_ID` and saves it to `~/.hermes/.env`:
   ```bash
   hermes plugins install TakeshiTGAL/hermes-plugin-estat --enable
   ```
   Once the plugin is listed in the Hermes plugin catalog, `hermes plugins install jp-estat --enable` works too. To try a local copy instead, run `hermes plugins install "file://$PWD" --enable` inside a clone of this repository.
3. Start a new Hermes session (run `hermes gateway restart` if you use the messaging gateway) and ask a question.

Works with Hermes Agent 0.18 or later (tested on 0.18.0 and on the main branch of 2 October 2026).

## What you can ask

- "How many people and households are there in Fuchu, Tokyo?" (市区町村の人口・世帯数)
- "How many establishments are there in Tokyo by industry?" (業種別の事業所数: business plans, subsidy applications, store-opening research)
- "Average monthly wages in construction, by prefecture" (業種別の賃金; 所定内給与額 = scheduled monthly pay, without overtime or bonuses)
- "How has the consumer price index moved over the last 12 months?" (消費者物価指数の推移)
- "Where does the number '14 million people in Tokyo' come from?" (記事・レポートの数字の出典)

## How it works

e-Stat holds hundreds of thousands of tables, and every row is addressed by codes (`13000`, `2020000000`, `0001`). The hard part is getting to the right table and the right codes. The plugin takes the agent there in two or three calls:

| Tool | What it does |
|---|---|
| `estat_search_tables` | Keyword search, narrowed by statistic (`国勢調査`), survey year and area level. Ranks the results, folds duplicates, and marks preliminary (速報) tables and series on an outdated base year. |
| `estat_table_info` | Lists a table's dimensions (areas, periods, measures, categories such as 男女 or 産業) with the names to use, and returns a ready-made example call. |
| `estat_get_data` | Fetches values **by name**: `area="東京都"`, `time="2020年"`, `filters={"男女": "女"}`. Returns a short table with unit, notes, survey date and citation. |

A typical run:

```text
estat_search_tables(query="男女別人口", survey="国勢調査", year="2020")
  → 0003445078  令和２年国勢調査 1-1-1 男女別人口－全国，都道府県，市区町村 …
estat_get_data(table_id="0003445078", area="東京都,全国", time="2020年")
  → fixed: 男女=総数, 時点=2020年   unit: 人
    rows: [["全国", 126146099], ["東京都", 14047594]]
    source.citation: 出典：政府統計の総合窓口(e-Stat)（https://www.e-stat.go.jp/）「令和２年国勢調査 …」（総務省）
```

### Naming things in `estat_get_data`

| Dimension | You can write |
|---|---|
| Area | `東京都`, `東京` (when unambiguous), `札幌市`, `東京都府中市` (prefecture first when a name exists twice), a code such as `13206`, several with commas, `children:東京都` (the areas directly under Tokyo), `level:2` (see `estat_table_info` for what each level holds), `all` |
| Time | `2020`, `2020年`, `令和2年`, `2026年8月`, `2026-08`, `2025年度`, `latest`, `latest:12` (last 12 periods), `latest year`, ranges `2024年1月~2026年8月` |
| Anything else | `filters={"男女": "女", "産業大分類": "建設業"}` by dimension name or key (`cat01`), item name with or without its leading code (`総合` matches `0001 総合`), `all` |

Anything you leave out has a sensible default, and the reply lists the defaults it used: classifications fall back to their total (総数, 総合, 全産業 …), the area to 全国, and the time to the latest period that has data for every requested row.

### What a reply contains

- `table`: id, statistic, title, organization, `survey_date` (when the survey was taken), `url` of the table on e-Stat, and `posted_on_e_stat` / `updated_on_e_stat` (when e-Stat posted or last updated the table, which is not the official release date of the statistic).
- `fixed`: the dimensions that have a single value, such as `{"男女": "総数", "時点": "2020年"}`; `columns` and `rows`: everything else; `unit`.
- `defaults_applied`: what the plugin filled in for you; `notes`: meaning of symbols such as `***`; `warnings`: things to tell the reader (see below).
- `source`: `citation` (出典 line), `citation_if_edited` (the extra line to add when you rework the numbers), `table_url` and the e-Stat `credit` sentence.

If a name matches nothing or more than one item, nothing is guessed: the reply lists the closest candidates with their codes (for example `府中市 (13206, 東京都)` and `府中市 (34208, 広島県)`). If a selection would return more than `max_rows` values (default 50), the reply explains which dimension to narrow instead of dumping a huge table.

### Things the plugin warns about

- **Preliminary tables (速報)** differ from the final release (確定). Search results say so.
- **Base years.** Index series such as the CPI are rebased every five years. When a newer base exists, older-base tables are marked and ranked lower, and data from them carries a warning, because current press releases use the new base.
- **Tokyo CPI before the national release.** The newest month for 東京都区部 is the mid-month preliminary estimate (中旬速報値) and is labelled as such.
- **Old data in the database.** Some statistics (wages, for example) reach the e-Stat database years after their newest results appear as Excel files. When the newest period a table has is old, the reply says so and links the statistic's file page.
- **Missing items.** If you ask for an item and e-Stat has no value for it in that selection, the reply names it instead of dropping it silently.
- **Raw vs seasonally adjusted.** e-Stat's CPI 前月比 is the raw change; press releases quote the seasonally adjusted one. Rows are labelled 前月比（原数値・季節調整なし）, and the reply points at the 季節調整済 item.

## Getting an application ID

1. Create an e-Stat account: <https://www.e-stat.go.jp/mypage/user/preregister> (free; a confirmation e-mail follows).
2. Log in, open **My Page (マイページ) → API機能（アプリケーションID発行）**, enter any name and, as the URL, `http://test.localhost/` (e-Stat accepts this for non-public use), then press **発行**.
3. Paste the ID when `hermes plugins install` asks for `ESTAT_APP_ID`, or add `ESTAT_APP_ID=<your ID>` to `~/.hermes/.env` yourself.

Official guide: <https://www.e-stat.go.jp/api/api-info/api-guide>.

## Credit and terms of use

このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。

*(e-Stat's English credit text: "This service uses API functions from e-Stat, however its contents are not guaranteed by government.")*

e-Stat asks every service built on its API to show the sentence above ([credit rule](https://www.e-stat.go.jp/api/api-info/credit)); the plugin also returns it with every data reply. Figures from e-Stat may be reused, including commercially, as long as the source is cited ([e-Stat terms](https://www.e-stat.go.jp/terms-of-use), compatible with CC BY 4.0). Each reply therefore carries a ready-made `citation` (出典：政府統計の総合窓口(e-Stat)…). When you rework the numbers (sums, ratios, charts), the terms ask you to say so separately: add the `citation_if_edited` line as well as the citation. API use is governed by the [e-Stat API terms](https://www.e-stat.go.jp/api/terms-of-use): the application ID is yours and must not be shared, and heavy bursts of requests are not allowed.

## Security and privacy

- All three tools are read-only.
- The plugin sends HTTPS requests to `api.e-stat.go.jp` only. It follows redirects only to `https://api.e-stat.go.jp` and refuses a redirect to another host or to plain HTTP, so the ID cannot be forwarded elsewhere.
- e-Stat requires the application ID as a query parameter (`appId`). The plugin never puts a request URL into an error, log line or tool reply, and removes the ID and any URL from network error text.
- It reads `ESTAT_APP_ID` and nothing else, writes no files, runs no shell commands or background processes, keeps two small in-memory caches (table metadata: 16 tables for 30 minutes; the base years available for a statistic: 1 hour), and does not update itself. No telemetry.
- Python standard library only; no dependencies.
- Load on e-Stat: one to five requests per tool call; table metadata is cached. A table search downloads up to 500 table descriptions for each statistic it searches, at most three when `survey` matches several, to rank them.
- Each tool call has a budget of about 45 seconds for all its requests (each network wait is capped at 20 seconds and no request starts after the budget is spent), then returns an error, so cron jobs and the messaging gateway do not hang on a slow e-Stat. Nothing waits for user input.

## Development

```bash
pip install "pytest>=8,<10" "pyyaml>=6,<7"
pytest -q
```

The tests replay trimmed real e-Stat responses from `tests/fixtures/` (sources listed in `tests/fixtures/README.md`), so they need no application ID and send no requests. To refresh the fixtures: `ESTAT_APP_ID=... python tests/record_fixtures.py`. Before publishing a pin: `hermes plugins validate . --install-deps`.

## License

MIT

---

## 日本語

e-Stat（政府統計の総合窓口）の API を活用して、政府統計を Hermes Agent から言葉で引けるようにしたプラグインです。e-Stat は便利ですが、目当ての統計表と分類コードにたどり着くまでが迷路です。このプラグインは、表の検索、表の中身（地域・時点・分類）の確認、名前での数値の取得を3つのツールに分け、エージェントが2〜3回の呼び出しで数字と出典にたどり着けるようにします。

### こんなときに

- 市区町村の人口や世帯数を確かめる
- 業種別の事業所数や賃金を調べる（企画書・補助金の申請・出店の検討）
- 消費者物価指数の動きを見る
- 記事やレポートに載せる数字の出典を取る

### 導入（3手）

1. e-Stat のアプリケーションID（無料）を取る（下の手順）
2. `hermes plugins install TakeshiTGAL/hermes-plugin-estat --enable` を実行し、聞かれたらIDを貼る（`~/.hermes/.env` に保存されます）。プラグインカタログに載った後は `hermes plugins install jp-estat --enable` でも入ります。手元のコピーで試すときは、clone したフォルダで `hermes plugins install "file://$PWD" --enable` を実行します
3. Hermes の新しいセッションで質問する（メッセージ連携の gateway を使っている場合は `hermes gateway restart`）

### アプリケーションIDの取り方

1. <https://www.e-stat.go.jp/mypage/user/preregister> でユーザ登録（無料。確認メールが届きます）
2. ログインして「マイページ」→「API機能（アプリケーションID発行）」を開き、名称は任意、URL は `http://test.localhost/`（公開サイトで使わない場合はこれで可）を入れて「発行」
3. 表示されたIDを、インストール時の入力欄か `~/.hermes/.env` の `ESTAT_APP_ID=` に入れる

### 名前で指定できます

`area="東京都"`、`time="令和2年"`、`filters={"男女": "女"}` のように、コードを知らなくても指定できます。同じ名前の市が2つある場合（府中市など）は、候補をコード付きで返します。`東京都府中市` のように都道府県名を前に付けても選べます。指定しなかった分類は「総数」などの合計、地域は「全国」、時点は「すべての行にデータがある最新の時点」になり、何を補ったかを返答に書きます。

### 注意すること

- 速報の表は確定値と数字が違います。検索結果に印を付けます。
- 消費者物価指数などの指数は5年ごとに基準年が変わります。新しい基準年の表があるときは、古い表に印を付け、そこから取った数字には警告を付けます。
- 東京都区部の消費者物価指数の最新月は「中旬速報値」です。返答にそう書きます。
- e-Stat の消費者物価指数の「前月比」は季節調整をしていない原数値です。報道発表の前月比は季節調整値なので、行の名前を「前月比（原数値・季節調整なし）」とし、季節調整済の品目の名前を返答で案内します。
- 賃金などは、新しい年の結果が先に Excel ファイルで公開され、データベースの表に入るのが何年も後になることがあります。表の最新の時点が古いときは、そう書いて統計のファイルのページを案内します。
- 指定した項目に値が無いときは、黙って消さずに名前を挙げます。
- 返答の `posted_on_e_stat` は e-Stat に表が載った日で、統計の公表日ではありません。

### クレジット表示

このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。

数字を使うときは、返答に入っている `citation`（出典：政府統計の総合窓口(e-Stat)…）をそのまま載せてください。数字を合計・割合・グラフなどに加工して使う場合は、出典とは別に加工したことを書く決まりなので、`citation` に加えて `citation_if_edited` の一文も載せます（[e-Stat 利用規約](https://www.e-stat.go.jp/terms-of-use)）。返答に `warnings` があるときは、その内容も読み手に伝えてください。

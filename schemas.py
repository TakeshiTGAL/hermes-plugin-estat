"""Tool schemas: what the model reads to decide when and how to call each tool."""

SEARCH_TABLES = {
    "name": "estat_search_tables",
    "description": (
        "Find official Japanese government statistics tables on e-Stat (政府統計の総合窓口, run by the "
        "Statistics Bureau of Japan). Use it whenever the user needs an official Japanese number: population "
        "or households of a prefecture or municipality, establishments or employees by industry, wages, "
        "consumer prices (CPI), and the source to cite for a figure. Step 1 of 2-3: search here, then "
        "estat_get_data (estat_table_info in between if you need to see the table's categories). "
        "Write keywords in Japanese, using the words that appear in table titles: '人口 世帯数', "
        "'事業所数 産業', '賃金 産業', '消費者物価指数'. Results are ranked best-first and marked when a "
        "table is preliminary (速報) or uses an older base year. Common sources: population → 国勢調査 "
        "(census, every 5 years, municipalities) or 人口推計 (yearly, prefectures); establishments by "
        "industry → 経済センサス‐活動調査; wages → 賃金構造基本統計調査 (by prefecture: query "
        "'都道府県別 年齢階級'); prices → 消費者物価指数. posted_on_e_stat is when e-Stat posted the "
        "table, not the official release date of the statistic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Japanese keywords; all must match (space-separated). OR is allowed: '東京 OR 大阪'.",
            },
            "survey": {
                "type": "string",
                "description": "Optional statistic name or code to search within, e.g. '国勢調査', '経済センサス‐活動調査', '00200521'.",
            },
            "year": {
                "type": "string",
                "description": "Optional survey year or range: '2020', '令和2年', '2020-2023', '202010'. Leave out for price and wage series, which often carry no survey year.",
            },
            "area_level": {
                "type": "string",
                "enum": ["national", "prefecture", "municipality"],
                "description": "Optional: only tables tabulated down to this area level.",
            },
            "limit": {
                "type": "integer",
                "description": "How many tables to return (default 10, max 30).",
            },
        },
        "required": ["query"],
    },
}

TABLE_INFO = {
    "name": "estat_table_info",
    "description": (
        "Show the dimensions of one e-Stat table and the names you can use in estat_get_data: areas "
        "(地域), periods (時点), measures (表章項目) and classifications such as 男女 or 産業. Use it after "
        "estat_search_tables when you are not sure which names a table uses, or to find the exact item "
        "among many (pass dimension and search, e.g. dimension='area', search='府中'). Returns a "
        "ready-to-use example call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table_id": {"type": "string", "description": "The 10-digit table id from estat_search_tables, e.g. '0003445078'."},
            "dimension": {"type": "string", "description": "Optional: list one dimension in full, by key ('area', 'time', 'cat01') or name ('男女')."},
            "search": {"type": "string", "description": "Optional: only items whose name contains this text."},
            "max_items": {"type": "integer", "description": "Items shown per dimension (default 12, or 100 with dimension; max 300)."},
        },
        "required": ["table_id"],
    },
}

GET_DATA = {
    "name": "estat_get_data",
    "description": (
        "Get numbers from an e-Stat table by name, without knowing e-Stat codes: area='東京都', "
        "time='2020年', filters={'男女': '総数'}. Returns a short table with the unit, notes, the survey "
        "date and the citation to show the user (出典). Anything you leave out defaults to the total "
        "(総数, 総合, 全産業 …), area to 全国 and time to the latest period that has data; the reply "
        "lists those defaults. Several values: 'A,B'. Time accepts '2020', '令和2年', '2026-08', "
        "'latest', 'latest:12' (last 12 periods) or a range '2024年1月~2026年8月'. Areas accept "
        "'children:東京都' (its municipalities) and 'level:2'. If a name is ambiguous (府中市 exists in "
        "two prefectures) the reply lists candidates with codes; call again with the code. When you "
        "report a number, always give the user the returned citation, and pass on every entry in "
        "warnings (preliminary figures, old base years, raw vs seasonally adjusted changes). CPI: "
        "headline = '総合', core = '生鮮食品を除く総合'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table_id": {"type": "string", "description": "The 10-digit table id from estat_search_tables."},
            "area": {"type": "string", "description": "Area name(s) or code(s): '東京都', '札幌市', '東京都,大阪府', 'children:東京都'."},
            "time": {"type": "string", "description": "Period: '2020年', '令和2年', '2026年8月', 'latest', 'latest:12', '2024年1月~2026年8月'."},
            "filters": {
                "type": "object",
                "description": "Other dimensions by name (or key such as 'cat01') → item name(s), e.g. {\"男女\": \"女\", \"産業分類\": \"建設業\"}.",
                "additionalProperties": {"type": "string"},
            },
            "max_rows": {"type": "integer", "description": "Largest table to return (default 50, max 500). Bigger selections return advice on narrowing instead."},
        },
        "required": ["table_id"],
    },
}

ALL_SCHEMAS = [SEARCH_TABLES, TABLE_INFO, GET_DATA]
BY_NAME = {s["name"]: s for s in ALL_SCHEMAS}

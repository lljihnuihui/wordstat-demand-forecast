from datetime import datetime, date, timedelta
from pathlib import Path
import shutil #to find the path to the clickhouse executable file in the system.
import subprocess
import json
import urllib.request
import urllib.error
import ssl
import certifi
#https://habr.com/ru/companies/ruvds/articles/440654/ - about argparse
import argparse

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_WORDSTAT_BASE_URL = "https://searchapi.api.cloud.yandex.net"
WORDSTAT_TOP_ENDPOINT = "/v2/wordstat/topRequests"
WORDSTAT_DYNAMICS_ENDPOINT = "/v2/wordstat/dynamics"
WORDSTAT_PERIOD_MONTH = 1
log_file = BASE_DIR / "update_wordstat.log"

#add string in the log file to see when did our scrip start and stop
def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")

def run_clickhouse_query(query, payload=None):
    ch = shutil.which("clickhouse")
    if ch is None:
        raise RuntimeError("clickhouse client was not found in PATH")

    subprocess.run(
        [ch, "client", "--query", query],
        input=payload.encode("utf-8") if payload is not None else None,
        check=True, # If clickhouse client is missing or the query fails, the script stops with an error.
    )

#read .env file and transform it into python dictionary

def load_env():
    env = {}
    text = ENV_FILE.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

def add_months(d, delta):
    #d.month - 1 to make January = 0, for instance
    #delta on how many months we need to shift
    total = d.year * 12 + (d.month - 1) + delta
    year = total // 12
    month = total % 12 + 1
    return date(year, month, 1)

def get_last_n_month_range(n_months=24):
    first_day_this_month = date.today().replace(day=1)

    #d = date(2026, 3, 1)
    #print(d - timedelta(days=1)) 2026-02-28
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    last_month_start = last_day_prev_month.replace(day=1)

    #let it be 2026-02-26, then last_month_start = 2026-01-01, n_month = 24 --> -(n_months - 1) = -23
    #from_date = add_months(date(2026, 1, 1), -23) -> 2024-02-01 which is exactly 24 months
    from_date = add_months(last_month_start, -(n_months - 1))
    return from_date, last_day_prev_month

def api_timestamp(d):
    return f"{d.isoformat()}T00:00:00Z"

'''
Send a POST request to the current Yandex Search API Wordstat endpoint.
Arguments:
endpoint: API endpoint, for example /v2/wordstat/topRequests.
api_key: Yandex Cloud API key from .env.
payload: JSON request body with phrase, dates and optional folder_id.
base_url: base API URL, usually https://searchapi.api.cloud.yandex.net.

Returns API response parsed as a Python dictionary.
'''
def wordstat_post(endpoint, api_key, payload, base_url):
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}{endpoint}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json;charset=utf-8",
        },
        method="POST",
    )
    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=90, context=ssl_ctx) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Yandex Search API HTTP {e.code}: {body}") from e

    except urllib.error.URLError as e:
        raise RuntimeError(f"Yandex Search API connection failed: {e}") from e

# Helper functions for parsing API responses.
# They make the loader more stable if Yandex changes field names slightly.
def find_lists(obj):
    if isinstance(obj, list):
        yield obj
        for item in obj:
            yield from find_lists(item)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from find_lists(value)

def get_env_required(env, name):
    value = env.get(name)
    if not value:
        raise RuntimeError(f"Missing {name} in {ENV_FILE}")
    return value
    
def pick_value(item, names):
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def to_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return int(str(value).replace(" ", "").replace("\u00a0", "").replace("\u202f", "").strip())

def to_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".").strip())


def normalize_month_date(value):
    if isinstance(value, dict):
        year = pick_value(value, ["year", "Year"])
        month = pick_value(value, ["month", "Month"])
        if year and month:
            return date(int(year), int(month), 1).isoformat()
    raw = str(value).strip()
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return raw
    
#Find the list of related search queries in the API response.
#Supports several possible response field names.
def extract_top_items(data):
    preferred = ["top_requests", "topRequests", "phrases", "items", "requests", "queries", "data"]
    if isinstance(data, dict):
        for key in preferred:
            value = data.get(key)
            if isinstance(value, list):
                return value

    for values in find_lists(data):
        if values and isinstance(values[0], dict):
            sample = values[0]
            if pick_value(sample, ["phrase", "text", "query", "request"]) is not None:
                return values
    return []

def extract_dynamic_items(data):
    preferred = ["dynamics", "dynamic", "items", "points", "data", "time_series", "timeSeries"]
    if isinstance(data, dict):
        for key in preferred:
            value = data.get(key)
            if isinstance(value, list):
                return value

    for values in find_lists(data):
        if values and isinstance(values[0], dict):
            sample = values[0]
            if pick_value(sample, ["date", "period", "month", "from_date", "fromDate"]) is not None:
                return values
    return []

#https://aistudio.yandex.ru/docs/ru/search-api/concepts/wordstat.html
#Request top related queries for the selected product and convert them to TSV
#for insertion into ClickHouse.
def build_top_payload_from_api(api_key, phrase, base_url, folder_id=None):
    #phrase = product, key phrase; numphrases = number of top req.
    body = {"phrase": phrase, 
            "num_phrases": 2000}
    if folder_id:
        body["folder_id"] = folder_id

    #data - python dict(API answer) from which we get top requests and frequency for ClickHouse
    data = wordstat_post(WORDSTAT_TOP_ENDPOINT, api_key, body, base_url)
    #meta to understand where did we get data
    meta = f"source=yandex_search_api;phrase={phrase}"

    rows = []
    for item in extract_top_items(data):
        if not isinstance(item, dict):
            continue
        q = str(pick_value(item, ["phrase", "text", "query", "request"]) or "").strip()
        cnt = to_int(pick_value(item, ["count", "requests", "shows", "number_of_queries", "numberOfQueries", "value"]))
        if q:
            rows.append(f"{q}\t{cnt}\t{meta}")

    if not rows:
        raise RuntimeError(f"Yandex Search API returned no top requests. Response keys: {list(data) if isinstance(data, dict) else type(data)}")

    return "\n".join(rows) + "\n", len(rows)

#Request monthly demand dynamics for the selected product and convert API rows
#to the ClickHouse TSV format used by product_monthly_actual.
def build_dynamic_payload_from_api(api_key, phrase, n_months=24, base_url=DEFAULT_WORDSTAT_BASE_URL, folder_id=None):
    from_date, to_date = get_last_n_month_range(n_months)

    body = {
        "phrase": phrase,
        "period": WORDSTAT_PERIOD_MONTH,
        "from_date": api_timestamp(from_date),
        "to_date": api_timestamp(to_date),
    }
    if folder_id:
        body["folder_id"] = folder_id

    data = wordstat_post(WORDSTAT_DYNAMICS_ENDPOINT, api_key, body, base_url)
    meta = f"source=yandex_search_api;phrase={phrase}"

    rows = []
    for item in extract_dynamic_items(data):
        if not isinstance(item, dict):
            continue
        month_date = normalize_month_date(pick_value(item, ["date", "period", "month", "from_date", "fromDate"]))
        cnt = to_int(pick_value(item, ["count", "requests", "shows", "number_of_queries", "numberOfQueries", "absolute", "absolute_value", "absoluteValue", "value"]))
        share = to_float(pick_value(item, ["share", "share_pct", "sharePct", "relative", "relative_value", "relativeValue"]), 0.0)
        if month_date:
            rows.append(f"{month_date}\t{cnt}\t{share}\t{meta}")

    if not rows:
        raise RuntimeError(f"Yandex Search API returned no dynamics. Response keys: {list(data) if isinstance(data, dict) else type(data)}")

    return "\n".join(rows) + "\n", len(rows)

#because '' in SQL is '
def sql_quote(value):
    return str(value).replace("'", "''")

def ensure_product_top_table():
    #A table for top-queries for each product (needed for the product filter in DataLens)
    run_clickhouse_query(
        """
        CREATE TABLE IF NOT EXISTS wordstat.product_top_queries
        (
            product String,
            query String,
            requests UInt64,
            dataset_meta String,
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY (product, query, loaded_at)
        """
    )

def build_product_top_payload(product, payload_top_raw):
    # from raw-payload "query\trequests\tmeta" make "product\tquery\trequests\tmeta"
    # and this is the “product label” for each row.
    # We are adding a product so that the system understands:
    # This line refers to sneakers, and this one refers to boots.
    rows = []
    for line in payload_top_raw.splitlines():
        line = line.strip()
        if line:
            rows.append(f"{product}\t{line}")
    return "\n".join(rows) + ("\n" if rows else ""), len(rows)

def build_product_actual_payload(product, payload_dyn_raw):
    #from raw-payload "month_date\trequests\tshare\tmeta" make "product\tmonth_date\trequests\tshare"
    rows = []
    for line in payload_dyn_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        month_date, cnt, share, _meta = line.split("\t", 3)
        rows.append(f"{product}\t{month_date}\t{cnt}\t{share}")
    return "\n".join(rows) + ("\n" if rows else ""), len(rows)

def main():
    #input in terminal months and product
    parser = argparse.ArgumentParser(description='Load product data from Yandex Search API Wordstat into ClickHouse')
    parser.add_argument("--product", default=None)
    parser.add_argument("--n-months", type=int, default=24)
    args = parser.parse_args()

    log("update started")
    env = load_env()

    api_key = get_env_required(env, "YANDEX_SEARCH_API_KEY")
    base_url = env.get("WORDSTAT_BASE_URL", DEFAULT_WORDSTAT_BASE_URL)
    folder_id = env.get("YANDEX_FOLDER_ID") or env.get("YC_FOLDER_ID")
    phrase = args.product

    payload_top, n_top = build_top_payload_from_api(api_key, phrase, base_url, folder_id=folder_id)
    payload_dyn, n_dyn = build_dynamic_payload_from_api(api_key, phrase, n_months=args.n_months, base_url=base_url, folder_id=folder_id)

    # 1) Product-specific storage
    ensure_product_top_table()
    payload_product_top, _ = build_product_top_payload(phrase, payload_top)
    payload_product_actual, _ = build_product_actual_payload(phrase, payload_dyn)

    phrase_sql = sql_quote(phrase)
    run_clickhouse_query(
        #ALTER TABLE to change already existing data in the table
        #delete old rows for a particular product
        #then NON-simultaniouslt insert fresh rows
        f"ALTER TABLE wordstat.product_top_queries DELETE WHERE product = '{phrase_sql}' SETTINGS mutations_sync = 1"
    )
    run_clickhouse_query(
        f"ALTER TABLE wordstat.product_monthly_actual DELETE WHERE product = '{phrase_sql}' SETTINGS mutations_sync = 1"
    )
    run_clickhouse_query(
        "INSERT INTO wordstat.product_top_queries (product, query, requests, dataset_meta) FORMAT TSV",
        payload_product_top
    )
    run_clickhouse_query(
        "INSERT INTO wordstat.product_monthly_actual (product, month_date, requests, share_pct) FORMAT TSV",
        payload_product_actual
    )
    print("insert ok")
    print(f"product: {phrase}")
    print(f"top rows: {n_top}")
    print(f"dynamic rows: {n_dyn}")
    print("ok")

if __name__ == "__main__":
    main()

from datetime import datetime, date, timedelta
from pathlib import Path
import csv
import io
import shutil #to find the path to the clickhouse executable file in the system.
import subprocess
import json
import urllib.request
import urllib.error
import ssl
import certifi
#https://habr.com/ru/companies/ruvds/articles/440654/ - about argparse
import argparse

ENV_FILE = Path("/Users/macbook/Desktop/pythonProject/.env")
WORDSTAT_BASE_URL = "https://api.wordstat.yandex.net"
log_file = Path("/Users/macbook/Desktop/pythonProject/update_wordstat.log")

#add string in the log file to see when did our scrip start and stop
def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")

def run_clickhouse_query(query, payload = None):
    ch = shutil.which('clickhouse')

    #build an array with arguments to launch comands in temrminal
    cmd = [ch, 'client', '--query', query]
    subprocess.run(
        cmd,
        input = payload.encode("utf-8") if payload is not None else None,
        check = True #if error erises we will see an error and not just silent breakdown
    )

#read .env file and transform it into python dictionary

def load_env():
    env = {}
    text = ENV_FILE.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env



def parse_regions(raw_value):
    raw_value = raw_value.strip().lower()
    if raw_value == "all":
        return []
    return [int(x.strip()) for x in raw_value.split(",") if x.strip()]



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
    to_date = last_day_prev_month
    return from_date.isoformat(), to_date.isoformat()

'''
Takes the endpoint (for example /v1/topRequests),
Takes the token (OAuth),
Takes the payload (request parameters),
Sends an HTTP POST with JSON,
Returns the API response as a Python dictionary (dict).
'''
def wordstat_post(endpoint, token, payload):
    #https://docs.python.org/3/library/urllib.request.html#urllib.request.Request
    req = urllib.request.Request(
        url=f"{WORDSTAT_BASE_URL}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=utf-8",
        },
        method="POST",
    )
    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=60, context=ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))


    #urllib.error (Python module, in urllib.error — Exception classes raised by urllib.request)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Wordstat API HTTP {e.code}: {body}") from e


#https://yandex.ru/support2/wordstat/ru/content/api-structure
def build_top_payload_from_api(token, phrase, regions_raw):
    regions = parse_regions(regions_raw)
    #phrase = product, key phrase; numphrases = number of top req.; devies = desktop + mobile(we can delete this feature)
    body = {"phrase": phrase,
            "numPhrases": 2000,
            "devices": ["all"]}
    if regions:
        body["regions"] = regions

    #data - python dict(API answer) from which we get top requests and frequency for ClickHouse
    data = wordstat_post("/v1/topRequests", token, body)
    #meta to understand where did we get data
    meta = f"source=wordstat_api;phrase={phrase}"

    rows = []
    for item in data.get("topRequests", []):
        q = str(item.get("phrase", "")).strip()
        cnt = int(item.get("count", 0))
        if q:
            rows.append(f"{q}\t{cnt}\t{meta}")

    payload = "\n".join(rows) + ("\n" if rows else "")
    return payload, len(rows)

def build_dynamic_payload_from_api(token, phrase, regions_raw, n_months=24):
    regions = parse_regions(regions_raw)
    from_date, to_date = get_last_n_month_range(n_months)

    body = {
        "phrase": phrase,
        "period": "monthly",
        "fromDate": from_date,
        "toDate": to_date,
        "devices": ["all"],
    }
    if regions:
        body["regions"] = regions

    data = wordstat_post("/v1/dynamics", token, body)
    meta = f"source=wordstat_api;phrase={phrase}"

    rows = []
    for item in data.get("dynamics", []):
        month_date = str(item.get("date", "")).strip()
        cnt = int(item.get("count", 0))
        share = float(item.get("share", 0.0))
        if month_date:
            rows.append(f"{month_date}\t{cnt}\t{share}\t{meta}")

    payload = "\n".join(rows) + ("\n" if rows else "")
    return payload, len(rows)

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
        if not line:
            continue
        rows.append(f"{product}\t{line}")
    payload = "\n".join(rows) + ("\n" if rows else "")
    return payload, len(rows)

def build_product_actual_payload(product: str, payload_dyn_raw: str):
    #from raw-payload "month_date\trequests\tshare\tmeta" make "product\tmonth_date\trequests\tshare"
    rows = []
    for line in payload_dyn_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        month_date, cnt, share, _meta = line.split("\t", 3)
        rows.append(f"{product}\t{month_date}\t{cnt}\t{share}")
    payload = "\n".join(rows) + ("\n" if rows else "")
    return payload, len(rows)

def main():
    #input in terminal months and product
    parser = argparse.ArgumentParser(description='Load product data from Wordstat API into ClickHouse')
    parser.add_argument("--product", default=None)
    parser.add_argument("--n-months", type=int, default=24)
    args = parser.parse_args()

    log("update started")
    env = load_env()

    token = env["WORDSTAT_TOKEN"]
    phrase = args.product
    regions_raw = env.get("WORDSTAT_REGION", "all")

    payload_top, n_top = build_top_payload_from_api(token, phrase, regions_raw)
    payload_dyn, n_dyn = build_dynamic_payload_from_api(token, phrase, regions_raw, n_months=args.n_months)

    # 1) Product-specific storage
    ensure_product_top_table()
    payload_product_top, n_top_product = build_product_top_payload(phrase, payload_top)
    payload_product_actual, n_actual_product = build_product_actual_payload(phrase, payload_dyn)

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

from datetime import datetime
from pathlib import Path
import csv
import io
import shutil #to find the path to the clickhouse executable file in the system.
import subprocess

top_csv = Path("/Users/macbook/Desktop/pythonProject/wordstat_top_queries.csv")
dyn_csv = Path("/Users/macbook/Desktop/pythonProject/wordstat_dynamic.csv")
log_file = Path("/Users/macbook/Desktop/pythonProject/update_wordstat.log")

#add string in the log file to see when did our scrip start and stop
def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")

def to_int(s):
  return int(s.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").strip())

#Prepare data to insert into ClickHouse
def build_top_payload():
    text = top_csv.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fields = reader.fieldnames
    query_col = fields[0]
    count_col = fields[1]
    meta_col = fields[2]
    rows = []
    for row in reader:
        q = (row.get(query_col)).strip()
        count = to_int(row.get(count_col))
        meta = (row.get(meta_col)).strip()
        rows.append(f"{q}\t{count}\t{meta}")
    payload = "\n".join(rows)
    return payload, len(rows)

months = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12
}
def to_float(s):
  return float(s.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".").strip())

def period_to_date(s):
  #февраль 2024 = 2024-02-01, strftime = string format time
  parts = str(s).strip().lower().split()
  month = months[parts[0]]
  year = int(parts[1])
  return datetime(year, month, 1).strftime("%Y-%m-%d")

def build_dynamic_payload():
    text = dyn_csv.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fields = reader.fieldnames
    period_col = fields[0]
    count_query_col = fields[1]
    share_col = fields[2]
    meta_col = fields[3]
    rows = []
    for row in reader:
        period = period_to_date(row.get(period_col))
        count = to_int(row.get(count_query_col))
        share = to_float(row.get(share_col))
        meta = (row.get(meta_col)).strip()
        rows.append(f"{period}\t{count}\t{share}\t{meta}")
    payload = '\n'.join(rows)
    return payload, len(rows)

def run_clickhouse_query(query, payload = None):
    ch = shutil.which('clickhouse')

    #build an array with arguments to launch comands in temrminal
    cmd = [ch, 'client', '--query', query]
    subprocess.run(
        cmd,
        input = payload.encode("utf-8") if payload is not None else None,
        check = True #if error erises we will see an error and not just silent breakdown
    )

def main():
    log("update started")
    log(f"top csv exists: {top_csv.exists()}")
    log(f"dynamic csv exists: {dyn_csv.exists()}")

    payload_top, n_top = build_top_payload()
    log(f"top payload rows: {n_top}")
    print("top rows:", n_top)

    payload_dyn, n_dyn = build_dynamic_payload()
    log(f"dynamic payload rows: {n_dyn}")
    print("dynamic rows:", n_dyn)

    #delete old tables to avoid duplicates
    run_clickhouse_query("TRUNCATE TABLE wordstat.top_queries_raw")
    run_clickhouse_query("TRUNCATE TABLE wordstat.dynamic_raw")

    #Insert top_queries_raw into the table,
    #Fill in the query, requests, dataset_meta columns,
    #Expect input data as TSV.

    run_clickhouse_query(
        "INSERT INTO wordstat.top_queries_raw (query, requests, dataset_meta) FORMAT TSV",
        payload_top
    )
    log(f"inserted top rows: {n_top}")

    run_clickhouse_query(
        "INSERT INTO wordstat.dynamic_raw (period_date, requests, share_pct, dataset_meta) FORMAT TSV",
        payload_dyn
    )
    log(f"inserted dynamic rows: {n_dyn}")


    print("insert ok")

    print("ok")

if __name__ == "__main__":
    main()

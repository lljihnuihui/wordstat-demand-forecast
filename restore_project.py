import subprocess
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CLICKHOUSE_DIR = PROJECT_DIR.parent / "clickhouse_wordstat"

DEFAULT_PRODUCT = "смартфон"
DEFAULT_MONTHS = 24

DATALENS_CONTAINERS = [
    "datalens-postgres",
    "datalens-control-api",
    "datalens-us",
    "datalens-data-api",
    "datalens-auth",
    "datalens-temporal",
    "datalens-ui-api",
    "datalens-ui",
    "datalens-meta-manager",
]


def run(cmd, check=True, cwd=None):
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(result.stdout)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def docker_is_running():
    result = run(["docker", "ps"], check=False)
    return result.returncode == 0


def start_docker_desktop():
    print("Docker is not running. Trying to open Docker Desktop...")
    run(["open", "-a", "Docker"], check=False)

    for _ in range(60):
        if docker_is_running():
            print("Docker is running.")
            return
        time.sleep(2)

    raise RuntimeError("Docker Desktop did not start. Open it manually and rerun this script.")


def start_datalens_containers():
    print("Starting DataLens containers if they exist...")

    for name in DATALENS_CONTAINERS:
        result = run(["docker", "inspect", name], check=False)

        if result.returncode == 0:
            run(["docker", "start", name], check=False)

    print("Waiting for DataLens containers...")
    time.sleep(10)


def clickhouse_http_ok():
    result = run(["curl", "-s", "http://localhost:8123"], check=False)
    return result.returncode == 0 and "Ok" in result.stdout


def clickhouse_client_ok():
    result = run(["clickhouse", "client", "--query", "SHOW DATABASES"], check=False)
    return result.returncode == 0


def start_clickhouse():
    if clickhouse_http_ok() and clickhouse_client_ok():
        print("ClickHouse is already running.")
        return

    print("ClickHouse is not running. Starting clickhouse server...")

    CLICKHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = PROJECT_DIR / "clickhouse_server.log"

    subprocess.Popen(
        ["clickhouse", "server"],
        cwd=CLICKHOUSE_DIR,
        stdout=log_file.open("a"),
        stderr=subprocess.STDOUT,
    )

    for _ in range(30):
        if clickhouse_http_ok() and clickhouse_client_ok():
            print("ClickHouse started.")
            return
        time.sleep(2)

    raise RuntimeError(f"ClickHouse did not start. Check log file: {log_file}")


def ch(query):
    return run(["clickhouse", "client", "--query", query])


def ensure_clickhouse_objects():
    print("Creating database, tables and views if missing...")

    ch("CREATE DATABASE IF NOT EXISTS wordstat")

    ch("""
    CREATE TABLE IF NOT EXISTS wordstat.product_monthly_actual
    (
        product String,
        month_date Date,
        requests UInt64,
        share_pct Float64,
        loaded_at DateTime DEFAULT now()
    )
    ENGINE = MergeTree
    ORDER BY (product, month_date)
    """)

    ch("""
    CREATE TABLE IF NOT EXISTS wordstat.product_monthly_forecast
    (
        product String,
        month_date Date,
        requests UInt64,
        model_version String,
        loaded_at DateTime DEFAULT now()
    )
    ENGINE = MergeTree
    ORDER BY (product, month_date)
    """)

    ch("""
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
    """)

    ch("""
    CREATE OR REPLACE VIEW wordstat.v_product_demand AS
    SELECT
        product,
        month_date,
        requests,
        'actual' AS series
    FROM wordstat.product_monthly_actual

    UNION ALL

    SELECT
        product,
        month_date,
        requests,
        'forecast' AS series
    FROM wordstat.product_monthly_forecast
    """)


def run_pipeline(product=DEFAULT_PRODUCT, months=DEFAULT_MONTHS):
    script = PROJECT_DIR / "run_product.sh"

    if not script.exists():
        raise FileNotFoundError(f"Not found: {script}")

    run(["chmod", "+x", str(script)], check=False)

    run([str(script), product, str(months)], cwd=PROJECT_DIR)


def check_result():
    ch("SHOW TABLES FROM wordstat")

    ch("""
    SELECT product, count(*)
    FROM wordstat.product_monthly_actual
    GROUP BY product
    """)

    ch("""
    SELECT product, count(*)
    FROM wordstat.product_monthly_forecast
    GROUP BY product
    """)

    ch("""
    SELECT product, count(*)
    FROM wordstat.product_top_queries
    GROUP BY product
    """)

    ch("""
    SELECT *
    FROM wordstat.v_product_demand
    LIMIT 5
    """)


def main():
    if not docker_is_running():
        start_docker_desktop()

    start_datalens_containers()
    start_clickhouse()
    ensure_clickhouse_objects()
    run_pipeline()
    check_result()

    print("\nDONE")
    print("Open DataLens:")
    print("http://localhost:8080")
    print("Then refresh the dashboard and select product = смартфон")


if __name__ == "__main__":
    main()

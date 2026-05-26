#!/usr/bin/env bash
set -euo pipefail

PY="/opt/miniconda3/envs/wordstat/bin/python"
DIR="$(cd "$(dirname "$0")" && pwd)"

PRODUCT="${1:?Usage: run_product.sh 'товар' [n_months]}"
N_MONTHS="${2:-24}"

"$PY" "$DIR/update_wordstat_api.py" --product "$PRODUCT" --n-months "$N_MONTHS"
"$PY" "$DIR/ML_predicter.py" --product "$PRODUCT"

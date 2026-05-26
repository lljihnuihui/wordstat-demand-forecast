import io
import math
import shutil
import subprocess
import argparse

import numpy as np
import pandas as pd
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

def parse_args():
    parser = argparse.ArgumentParser(description="Train forecast for one product")
    parser.add_argument("--product", default="смартфон")
    return parser.parse_args()

def sql_quote(value):
    return str(value).replace("'", "''")

ARGS = parse_args()
PRODUCT = ARGS.product
PRODUCT_SQL = sql_quote(PRODUCT)

#https://robjhyndman.com/hyndsight/wape.html(wape + mase)
def wape(y_true, y_pred):
    denominator = np.sum(np.abs(y_true))
    if denominator == 0:
        return 0.0
    return float(np.sum(np.abs(y_true - y_pred)) / denominator * 100)

def mase(y_true, y_pred, y_insample, seasonality=1):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    y_insample = np.asarray(y_insample, dtype=float)

    scale = np.mean(np.abs(y_insample[seasonality:] - y_insample[:-seasonality]))
    if scale == 0:
        return float("nan")

    return float(np.mean(np.abs(y_true - y_pred)) / scale)


# find the path to the clickhouse
def ch():
    return shutil.which('clickhouse')


# Apply SQL in ClickHouse and return text as pandas.DataFrame
def query_df(sql: str):
    result = subprocess.check_output([ch(), 'client', '--query', sql], text=True)
    return pd.read_csv(io.StringIO(result))


# f"""...""" is needed to insert the value of the PRODUCT variable inside SQL.
sql = f"""
SELECT month_date, requests
FROM wordstat.product_monthly_actual
WHERE product = '{PRODUCT_SQL}'
ORDER BY month_date
FORMAT CSVWithNames
"""

df = query_df(sql)
df['month_date'] = pd.to_datetime(df['month_date'])
df['requests'] = pd.to_numeric(df['requests'], errors='coerce')
print(df.head())
print('rows:', len(df))

# lags - past row values for forecasts
LAGS = 6

y = df['requests'].astype(float).values
m = df['month_date'].dt.month.values

X_train = []
y_train = []

for i in range(LAGS, len(y)):
    # lag1...lag6 last 6 values of row
    lags = y[i - LAGS:i][::-1]

    '''
    cyclical encoding in order to improve seasonality(make points december and january near, not 12 and 1)
    January (m=1):
    θ = 2π * 1/12 = π/6
    sin ≈ 0.5, cos ≈ 0.866

    June (m=6):
    θ = π
    sin = 0, cos = -1

    December (m=12):
    θ = 2π
    sin = 0, cos = 1
    '''

    sin_m = math.sin(2 * math.pi * m[i] / 12)
    cos_m = math.cos(2 * math.pi * m[i] / 12)
    X_train.append(np.concatenate([lags, [sin_m, cos_m]]))
    # expected result
    y_train.append(y[i])

X_train = np.array(X_train)
y_train = np.array(y_train)

print('X_train shape:', X_train.shape)
print('y_train shape:', y_train.shape)

y_train_log = np.log1p(y_train)  # log(1 + x)

# https://scikit-learn.org/stable/modules/generated/sklearn.neural_network.MLPRegressor.html
model = Pipeline([
    ('scaler', StandardScaler()),
    ('mlp', MLPRegressor(
        hidden_layer_sizes=(32, 16),
        activation='tanh',
        solver='adam',
        random_state=42,
        max_iter=5000,
        early_stopping=True,
        n_iter_no_change=30
    ))
])

print("train feature count:", X_train.shape[1])
model.fit(X_train, y_train_log)

# validate on last 6 points of feature dataset
VAL = 6
split = len(X_train) - VAL
X_tr, X_val = X_train[:split], X_train[split:]
y_tr, y_val = y_train[:split], y_train[split:]

# retrain MLP on train-only for fair validation
model.fit(X_tr, np.log1p(y_tr))
pred_mlp = np.expm1(model.predict(X_val))
pred_mlp = np.maximum(pred_mlp, 0.0)

# seasonal naive baseline (same month last year)
val_orig_idx = np.arange(LAGS + split, LAGS + len(X_train))
base_pred = []
for idx in val_orig_idx:
    base_pred.append(y[idx - 12] if idx >= 12 else y[idx - 1])
base_pred = np.array(base_pred, dtype=float)
w_mlp = wape(y_val, pred_mlp)
w_base = wape(y[val_orig_idx], base_pred)
print(f"WAPE MLP: {w_mlp:.2f}%")
print(f"WAPE BASE: {w_base:.2f}%")

train_end_idx = LAGS + split
y_insample = y[:train_end_idx]
if len(y_insample) > 12:
    seasonality = 12
else:
    seasonality = 1
m_mlp = mase(y_val, pred_mlp, y_insample, seasonality=seasonality)
m_base = mase(y[val_orig_idx], base_pred, y_insample, seasonality=seasonality)

print(f"MASE MLP: {m_mlp:.3f}")
print(f"MASE BASE: {m_base:.3f}")


use_mlp = w_mlp <= w_base
print("Selected:", "MLP" if use_mlp else "SEASONAL_NAIVE")

# now train MLP on full dataset for final forecast if selected
if use_mlp:
    model.fit(X_train, np.log1p(y_train))
    MODEL_VERSION = "mlp_lag6_log1p_v4"
else:
    MODEL_VERSION = "seasonal_naive_lag12_v1"



history = list(y.astype(float))
last_month = df['month_date'].iloc[-1]

forecast_rows = []

for step in range(1, 13):
    # DateOffset it moves correctly by calendar months.
    future_date = last_month + pd.DateOffset(months=step)
    month_num = future_date.month

    sin_m = math.sin(2 * math.pi * month_num / 12)
    cos_m = math.cos(2 * math.pi * month_num / 12)

    '''
    MLPRegressor.predict() waiting for a 2D array of the shape:
    (n_samples, n_features).
    n_samples = 1 (we predict one next month),
    n_features = 8 (6 lags + sin + cos).
    '''

    lags = np.array(history[-LAGS:][::-1], dtype=float)
    x = np.concatenate([lags, [sin_m, cos_m]]).reshape(1, -1)

    if use_mlp:
        pred_log = float(model.predict(x)[0])
        pred = float(np.expm1(pred_log))
        pred = max(pred, 0.0)
    else:
        pred = float(history[-12]) if len(history) >= 12 else float(history[-1])

    history.append(pred)
    forecast_rows.append((future_date.strftime('%Y-%m-%d'), int(round(pred))))

fcst_df = pd.DataFrame(forecast_rows, columns=['month_date', 'requests'])
print(fcst_df)

''' 
mlp — neural network MLP,
lag6 — used 6 lags,
log1p — The target was log-converted,
v1 — first version.
'''
if use_mlp:
    MODEL_VERSION = "mlp_lag6_log1p_v4"
else:
    MODEL_VERSION = "seasonal_naive_lag12_v1"



# clean previous forecast for the product
subprocess.run(
    [
        ch(),
        'client',
        '--query',
        # SETTINGS mutations_sync = 1 to delete the old forecast before inserting the new one.
        f"ALTER TABLE wordstat.product_monthly_forecast DELETE WHERE product = '{PRODUCT_SQL}' SETTINGS mutations_sync = 1"

    ],
    check=True
)

# forming a TSV package
payload = '\n'.join(
    f"{PRODUCT}\t{row.month_date}\t{int(row.requests)}\t{MODEL_VERSION}"
    for row in fcst_df.itertuples(index=False)
) + '\n'

# insert new forecast
subprocess.run(
    [
        ch(), 'client', '--query',
        'INSERT INTO wordstat.product_monthly_forecast(product, month_date, requests, model_version) FORMAT TSV'
    ],
    input=payload.encode('utf-8'),
    check=True
)

print('forecast inserted', len(fcst_df))

check_sql = f"""
SELECT product, month_date, requests, model_version
FROM wordstat.product_monthly_forecast
WHERE product = '{PRODUCT_SQL}'
ORDER BY month_date
FORMAT CSVWithNames
"""

#print(query_df(check_sql).head(12))
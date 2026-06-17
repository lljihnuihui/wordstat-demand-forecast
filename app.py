import os
import re
import uuid
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


PROJECT_DIR = Path(
    os.getenv("WORDSTAT_PROJECT_DIR", "/Users/macbook/Desktop/wordstat_project")
).resolve()

PYTHON_BIN = os.getenv("WORDSTAT_PYTHON", "/opt/miniconda3/envs/wordstat/bin/python")
UPDATE_SCRIPT = PROJECT_DIR / "update_wordstat_api.py"
PREDICT_SCRIPT = PROJECT_DIR / "ML_predicter.py"

DASHBOARD_URL = os.getenv(
    "DATALENS_DASHBOARD_URL",
    "http://localhost:8080/heo0t3lru54y1-project-2nd-course",
)

MAX_WORKERS = int(os.getenv("WORDSTAT_MAX_WORKERS", "1"))  

app = FastAPI(title="Wordstat Product API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
jobs_lock = threading.Lock()
jobs: Dict[str, dict] = {}


class AnalyzeRequest(BaseModel):
    product: str = Field(..., min_length=1, max_length=120, description="Any product text")
    n_months: int = Field(24, ge=12, le=60)


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str
    status_url: str
    dashboard_url: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    product: str
    n_months: int
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    logs: Optional[str] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_product(value: str) -> str:
    value = " ".join(value.strip().split())
    # Remove dangerous control chars
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    return value


def ensure_files_exist() -> None:
    missing = []
    for p in [UPDATE_SCRIPT, PREDICT_SCRIPT]:
        if not p.exists():
            missing.append(str(p))
    if missing:
        raise RuntimeError("Missing required files: " + ", ".join(missing))


def set_job(job_id: str, **kwargs) -> None:
    with jobs_lock:
        jobs[job_id].update(kwargs)


def run_pipeline(job_id: str, product: str, n_months: int) -> None:
    set_job(job_id, status="running", started_at=now_iso())

    cmd_update = [
        PYTHON_BIN,
        str(UPDATE_SCRIPT),
        "--product", product,
        "--n-months", str(n_months),
    ]
    cmd_predict = [
        PYTHON_BIN,
        str(PREDICT_SCRIPT),
        "--product", product,
    ]

    try:
        out_parts = []

        p1 = subprocess.run(
            cmd_update,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            check=True,
        )
        out_parts.append("=== update_wordstat_api.py ===\n" + (p1.stdout or "") + (p1.stderr or ""))

        p2 = subprocess.run(
            cmd_predict,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            check=True,
        )
        out_parts.append("=== ML_predicter.py ===\n" + (p2.stdout or "") + (p2.stderr or ""))

        set_job(
            job_id,
            status="done",
            finished_at=now_iso(),
            logs="\n".join(out_parts)[-15000:], 
        )
    except subprocess.CalledProcessError as e:
        err = (
            f"Command failed: {e.cmd}\n"
            f"Return code: {e.returncode}\n"
            f"STDOUT:\n{e.stdout or ''}\n"
            f"STDERR:\n{e.stderr or ''}"
        )
        set_job(
            job_id,
            status="failed",
            finished_at=now_iso(),
            error=err[-10000:],
            logs=err[-15000:],
        )
    except Exception as e:
        set_job(
            job_id,
            status="failed",
            finished_at=now_iso(),
            error=str(e),
        )


@app.get("/api/health")
def health():
    try:
        ensure_files_exist()
        return {"ok": True, "project_dir": str(PROJECT_DIR)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    ensure_files_exist()

    product = normalize_product(req.product)
    if not product:
        raise HTTPException(status_code=400, detail="Product is empty after normalization")

    with jobs_lock:
        for jid, meta in jobs.items():
            if meta["status"] in {"queued", "running"} and meta["product"] == product:
                return AnalyzeResponse(
                    job_id=jid,
                    status=meta["status"],
                    status_url=f"/api/jobs/{jid}",
                    dashboard_url=DASHBOARD_URL,
                )

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "product": product,
            "n_months": req.n_months,
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "logs": None,
        }

    executor.submit(run_pipeline, job_id, product, req.n_months)

    return AnalyzeResponse(
        job_id=job_id,
        status="queued",
        status_url=f"/api/jobs/{job_id}",
        dashboard_url=DASHBOARD_URL,
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(**job)


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        data = list(jobs.values())
    data.sort(key=lambda x: x["created_at"], reverse=True)
    return data


@app.get("/", response_class=HTMLResponse)
def ui():
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Wordstat Any Product</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 16px; max-width: 1400px; }}
    .row {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }}
    input, button {{ padding: 8px; font-size: 14px; }}
    #product {{ flex: 1; min-width: 260px; }}
    #months {{ width: 90px; }}
    #status {{ margin: 8px 0; white-space: pre-wrap; }}
    iframe {{ width: 100%; height: 78vh; border: 1px solid #ccc; }}
  </style>
</head>
<body>
  <h2>Wordstat: Any Product</h2>

  <div class="row">
    <input id="product" placeholder="Type any product (e.g. ноутбук игровой)" />
    <input id="months" type="number" min="12" max="60" value="24" />
    <button onclick="startAnalyze()">Analyze</button>
    <button onclick="reloadDashboard()">Reload dashboard</button>
  </div>

  <div id="status">Idle</div>

  <iframe id="dash" src="{DASHBOARD_URL}"></iframe>

<script>
let pollTimer = null;

async function startAnalyze() {{
  const product = document.getElementById('product').value.trim();
  const n_months = Number(document.getElementById('months').value || 24);

  if (!product) {{
    setStatus('Please type product name');
    return;
  }}

  setStatus('Submitting job...');

  const r = await fetch('/api/analyze', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ product, n_months }})
  }});

  const data = await r.json();

  if (!r.ok) {{
    setStatus('Error: ' + JSON.stringify(data));
    return;
  }}

  setStatus(`Job ${{data.job_id}}: ${{data.status}}`);
  pollJob(data.job_id, product);
}}

async function pollJob(jobId, product) {{
  if (pollTimer) clearInterval(pollTimer);

  pollTimer = setInterval(async () => {{
    const r = await fetch('/api/jobs/' + jobId);
    const j = await r.json();

    setStatus(
      `Product: ${{j.product}}\n` +
      `Status: ${{j.status}}\n` +
      `Created: ${{j.created_at}}\n` +
      (j.started_at ? `Started: ${{j.started_at}}\n` : '') +
      (j.finished_at ? `Finished: ${{j.finished_at}}\n` : '') +
      (j.error ? `Error:\n${{j.error}}\n` : '')
    );

    if (j.status === 'done') {{
      clearInterval(pollTimer);
      reloadDashboard();
      setStatus(
        `Done for "${{product}}". Dashboard refreshed.\n` +
        `Now select product = "${{product}}" in the product filter.`
      );
    }}

    if (j.status === 'failed') {{
      clearInterval(pollTimer);
    }}
  }}, 2500);
}}

function reloadDashboard() {{
  const iframe = document.getElementById('dash');
  iframe.src = iframe.src.split('?')[0] + '?t=' + Date.now();
}}

function setStatus(txt) {{
  document.getElementById('status').textContent = txt;
}}
</script>
</body>
</html>
"""
    return HTMLResponse(html)


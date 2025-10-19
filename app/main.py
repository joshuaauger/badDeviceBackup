import os
import sys
import time
import json
import signal
import queue
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Response, status, Body
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================
# Environment / Configuration
# ============================================================
APP_MODE = os.getenv("APP_MODE", "full").lower()  # "full" or "backup-only"
PORT = int(os.getenv("PORT", "8080"))
BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
LOCKDOWN_DIR = os.getenv("LOCKDOWN_DIR", "/var/lib/lockdown")
OPENSSL_WEAK_CONFIG = os.getenv("OPENSSL_WEAK_CONFIG", "/data/config/openssl-weak.conf")
USBMUXD_SOCKET_ADDRESS = os.getenv("USBMUXD_SOCKET_ADDRESS", "127.0.0.1:27015")
NETMUXD_BIN = os.getenv("NETMUXD_BIN", "netmuxd")  # allow override
NETMUXD_DISABLE_UNIX = os.getenv("NETMUXD_DISABLE_UNIX", "true").lower() == "true"
NETMUXD_HOST = os.getenv("NETMUXD_HOST", "127.0.0.1")
START_NETMUXD = APP_MODE != "backup-only"

# Maximum simultaneous backups (per system)
MAX_CONCURRENT_BACKUPS = int(os.getenv("MAX_CONCURRENT_BACKUPS", "2"))


# ============================================================
# Data Structures
# ============================================================
class PairRequest(BaseModel):
    udid: Optional[str] = None


class UnpairRequest(BaseModel):
    udid: str


class BackupRequest(BaseModel):
    udid: str
    mode: Optional[str] = "full"  # placeholder for future expansion
    incremental: Optional[bool] = (
        True  # if false we might do a "fresh" backup (currently idevicebackup2 flags control)
    )


class BackupJobStatus(BaseModel):
    job_id: str
    udid: str
    status: str
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    exit_code: Optional[int]
    error: Optional[str]
    line_count: int
    progress_hint: Optional[float]


# Internal job representation
class BackupJob:
    def __init__(self, udid: str, incremental: bool):
        self.job_id = str(uuid.uuid4())
        self.udid = udid
        self.incremental = incremental
        self.status = "queued"
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.error = None
        self.logs: List[str] = []
        self._lock = threading.Lock()
        self.progress_hint: Optional[float] = None  # rough progress estimate (0..1)
        self.proc: Optional[subprocess.Popen] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "udid": self.udid,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "line_count": len(self.logs),
            "progress_hint": self.progress_hint,
        }


# ============================================================
# Globals
# ============================================================
netmuxd_process: Optional[subprocess.Popen] = None
jobs: Dict[str, BackupJob] = {}
jobs_queue: "queue.Queue[BackupJob]" = queue.Queue()
active_backup_threads: Dict[str, threading.Thread] = {}
device_locks: Dict[str, threading.Lock] = {}
stop_event = threading.Event()


# ============================================================
# Utility Functions
# ============================================================
def ensure_directories():
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    Path(LOCKDOWN_DIR).mkdir(parents=True, exist_ok=True)
    Path(OPENSSL_WEAK_CONFIG).parent.mkdir(parents=True, exist_ok=True)


def ensure_weak_openssl_config():
    if not Path(OPENSSL_WEAK_CONFIG).exists():
        base = "/etc/ssl/openssl.cnf"
        if not Path(base).exists():
            raise RuntimeError(f"System OpenSSL config '{base}' not found.")
        content = f""".include {base}
[openssl_init]
alg_section = evp_properties
[evp_properties]
rh-allow-sha1-signatures = yes
"""
        Path(OPENSSL_WEAK_CONFIG).write_text(content)


def run_cmd(
    cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: Optional[int] = None
) -> str:
    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, env=env, timeout=timeout, text=True
        )
        return output
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Command failed (rc={e.returncode}): {cmd}\nOutput:\n{e.output}"
        )


def list_device_udids() -> List[str]:
    try:
        out = run_cmd(["idevice_id", "-l"]).strip()
        if not out:
            return []
        return out.splitlines()
    except Exception:
        return []


def device_info(udid: str) -> Dict[str, Any]:
    info = {}
    try:
        raw = run_cmd(["ideviceinfo", "-u", udid])
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
    except Exception as e:
        info["_error"] = str(e)
    return info


def is_paired(udid: str) -> bool:
    # paired if lockdown plist exists
    plist_path = Path(LOCKDOWN_DIR) / f"{udid}.plist"
    return plist_path.exists()


def start_netmuxd():
    global netmuxd_process
    if netmuxd_process is not None:
        return
    args = [NETMUXD_BIN, "--host", NETMUXD_HOST]
    if NETMUXD_DISABLE_UNIX:
        args.insert(1, "--disable-unix")
    netmuxd_process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    threading.Thread(target=_netmuxd_log_reader, daemon=True).start()


def _netmuxd_log_reader():
    if not netmuxd_process or not netmuxd_process.stdout:
        return
    for line in netmuxd_process.stdout:
        sys.stdout.write(f"[netmuxd] {line}")
    rc = netmuxd_process.wait()
    sys.stdout.write(f"[netmuxd] exited rc={rc}\n")


def shutdown_netmuxd():
    global netmuxd_process
    if netmuxd_process and netmuxd_process.poll() is None:
        netmuxd_process.terminate()
        try:
            netmuxd_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            netmuxd_process.kill()
    netmuxd_process = None


def acquire_device_lock(udid: str):
    lock = device_locks.get(udid)
    if lock is None:
        lock = threading.Lock()
        device_locks[udid] = lock
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Device {udid} already in use by another operation.",
        )


def release_device_lock(udid: str):
    lock = device_locks.get(udid)
    if lock and lock.locked():
        lock.release()


# ============================================================
# Backup Worker
# ============================================================
def backup_worker():
    """Central queue consumer (optional if we want sequential).
    Currently we allow multiple threads so this may remain unused unless queue logic is needed.
    """
    while not stop_event.is_set():
        try:
            job = jobs_queue.get(timeout=1)
        except queue.Empty:
            continue
        if stop_event.is_set():
            break
        # Start separate thread
        t = threading.Thread(target=run_backup_job, args=(job,), daemon=True)
        active_backup_threads[job.job_id] = t
        t.start()


def run_backup_job(job: BackupJob):
    udid = job.udid
    try:
        acquire_device_lock(udid)
    except HTTPException as he:
        job.status = "error"
        job.error = he.detail
        job.finished_at = time.time()
        return
    job.status = "running"
    job.started_at = time.time()

    env = os.environ.copy()
    env["OPENSSL_CONF"] = OPENSSL_WEAK_CONFIG
    env["USBMUXD_SOCKET_ADDRESS"] = USBMUXD_SOCKET_ADDRESS

    # Command line: incremental implied by existing backup dir; we always pass -n --full
    # Directory structure: idevicebackup2 places UDID directory inside BACKUP_DIR automatically.
    cmd = ["idevicebackup2", "backup", "-n", "--full", BACKUP_DIR]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        job.proc = proc
        line_total_estimate = 0  # naive heuristic
        for line in proc.stdout:
            line = line.rstrip()
            job.logs.append(line)
            # Heuristic: attempt to extract counts for progress
            # idevicebackup2 output isn't strictly documented; we use line count heuristic
            line_total_estimate += 1
            # Provide pseudo progress (arbitrary cap)
            job.progress_hint = min(0.95, line_total_estimate / 5000.0)
        rc = proc.wait()
        job.exit_code = rc
        job.finished_at = time.time()
        if rc == 0:
            job.status = "success"
            job.progress_hint = 1.0
        else:
            job.status = "error"
            job.error = f"Backup process exited with code {rc}"
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.finished_at = time.time()
    finally:
        release_device_lock(udid)


# ============================================================
# FastAPI Application
# ============================================================
app = FastAPI(title="iOS Network Backup Service", version="0.1.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ============================================================
# Startup / Shutdown
# ============================================================
@app.on_event("startup")
def on_startup():
    ensure_directories()
    ensure_weak_openssl_config()
    if START_NETMUXD:
        start_netmuxd()
    # Start worker thread (currently optional)
    threading.Thread(target=backup_worker, daemon=True).start()


@app.on_event("shutdown")
def on_shutdown():
    stop_event.set()
    shutdown_netmuxd()


# ============================================================
# API Endpoints
# ============================================================


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app_mode": APP_MODE,
        "netmuxd_running": netmuxd_process is not None
        and netmuxd_process.poll() is None,
        "jobs_active": len([j for j in jobs.values() if j.status == "running"]),
        "devices": list_device_udids(),
    }


@app.get("/devices")
def get_devices():
    udids = list_device_udids()
    resp = []
    for u in udids:
        resp.append({"udid": u, "paired": is_paired(u)})
    return resp


@app.get("/devices/{udid}/info")
def get_device_info(udid: str):
    if udid not in list_device_udids():
        raise HTTPException(status_code=404, detail=f"Device {udid} not detected.")
    return {"udid": udid, "paired": is_paired(udid), "info": device_info(udid)}


@app.post("/pair")
def pair_device(req: PairRequest | None = Body(default=None)):
    udids = list_device_udids()
    target = req.udid if req else None
    if target:
        if target not in udids:
            raise HTTPException(
                status_code=404, detail=f"Device {target} not detected."
            )
    else:
        if not udids:
            raise HTTPException(status_code=404, detail="No devices connected.")
        if len(udids) > 1:
            raise HTTPException(
                status_code=400, detail="Multiple devices connected; specify UDID."
            )
        target = udids[0]

    if is_paired(target):
        return {"udid": target, "status": "already_paired"}

    env = os.environ.copy()
    env["OPENSSL_CONF"] = OPENSSL_WEAK_CONFIG
    try:
        output = run_cmd(["idevicepair", "pair"], env=env, timeout=60)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"udid": target, "status": "paired", "output": output}


@app.post("/unpair")
def unpair_device(req: UnpairRequest):
    plist = Path(LOCKDOWN_DIR) / f"{req.udid}.plist"
    if not plist.exists():
        raise HTTPException(status_code=404, detail="Device not paired.")
    try:
        plist.unlink()
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to remove lockdown file: {e}"
        )
    return {"udid": req.udid, "status": "unpaired"}


@app.post("/backup")
def start_backup(req: BackupRequest):
    udid = req.udid
    if udid not in list_device_udids():
        raise HTTPException(status_code=404, detail=f"Device {udid} not detected.")
    if not is_paired(udid):
        raise HTTPException(status_code=400, detail=f"Device {udid} not paired.")

    # Check concurrency limits
    running_jobs = [j for j in jobs.values() if j.status == "running"]
    if len(running_jobs) >= MAX_CONCURRENT_BACKUPS:
        raise HTTPException(
            status_code=429, detail="Too many concurrent backups; try again later."
        )

    # Prevent duplicate job if one already queued/running for same device
    for j in jobs.values():
        if j.udid == udid and j.status in ("queued", "running"):
            raise HTTPException(
                status_code=409,
                detail=f"Backup already in progress for {udid} (job {j.job_id}).",
            )

    job = BackupJob(udid=udid, incremental=req.incremental)
    jobs[job.job_id] = job

    # Enqueue job for worker (or start direct thread)
    # For immediate start, push to queue:
    jobs_queue.put(job)

    return {"job_id": job.job_id, "status": job.status, "udid": udid}


@app.get("/backup/jobs")
def list_backup_jobs():
    return [j.to_dict() for j in jobs.values()]


@app.get("/backup/{job_id}/status", response_model=BackupJobStatus)
def get_job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return BackupJobStatus(**job.to_dict())


@app.get("/backup/{job_id}/logs")
def get_job_logs(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "line_count": len(job.logs),
        "logs": job.logs[-500:],  # limit
    }


@app.get("/backup/{job_id}/stream")
def stream_job_logs(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    def event_stream():
        last_index = 0
        while True:
            if job.status in ("success", "error") and last_index >= len(job.logs):
                # send final event then break
                yield f"event:done\ndata:{json.dumps({'status': job.status})}\n\n"
                break
            while last_index < len(job.logs):
                line = job.logs[last_index]
                last_index += 1
                payload = json.dumps(
                    {"line": line, "index": last_index, "status": job.status}
                )
                yield f"data:{payload}\n\n"
            time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/backups/{udid}")
def list_backups_for_device(udid: str):
    target_dir = Path(BACKUP_DIR) / udid
    if not target_dir.exists():
        return {"udid": udid, "exists": False, "size_bytes": 0, "file_count": 0}
    size = 0
    count = 0
    for root, dirs, files in os.walk(target_dir):
        for f in files:
            fp = Path(root) / f
            try:
                size += fp.stat().st_size
            except Exception:
                pass
            count += 1
    return {"udid": udid, "exists": True, "size_bytes": size, "file_count": count}


@app.get("/backups/{udid}/files")
def list_backup_files(udid: str):
    target_dir = Path(BACKUP_DIR) / udid
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Backup directory not found.")
    files_meta = []
    for root, dirs, files in os.walk(target_dir):
        for f in files:
            fp = Path(root) / f
            rel = fp.relative_to(target_dir)
            try:
                sz = fp.stat().st_size
            except Exception:
                sz = None
            files_meta.append({"path": str(rel), "size": sz})
    return {"udid": udid, "files": files_meta}


@app.delete("/backups/{udid}")
def delete_device_backup(udid: str):
    target_dir = Path(BACKUP_DIR) / udid
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Backup directory not found.")
    try:
        shutil.rmtree(target_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete backup: {e}")
    return {"udid": udid, "status": "deleted"}


@app.get("/ui", response_class=HTMLResponse)
def ui_dashboard():
    """
    Serve the bundled static HTML dashboard (templates/index.html).
    """
    template_path = Path(__file__).parent / "templates" / "index.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard template missing.")
    try:
        content = template_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to read dashboard template: {e}"
        )
    return HTMLResponse(content=content, status_code=200)


# ============================================================
# Graceful Signal Handling (if run directly)
# ============================================================
def _signal_handler(sig, frame):
    stop_event.set()
    shutdown_netmuxd()
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ============================================================
# Entry Point when executed directly (uvicorn alternative)
# ============================================================
if __name__ == "__main__":
    # When invoked directly, run uvicorn
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)

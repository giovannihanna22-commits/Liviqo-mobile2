from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from threading import Thread, Event, Lock
from collections import deque
from urllib.parse import urlparse
import webbrowser, os, sys, traceback

import dubizzle_scraper1 as scraper

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
app = FastAPI(title="LIVIQO Dubizzle Owner Finder")
app.mount("/static", StaticFiles(directory=ROOT/"static"), name="static")

lock = Lock()
resume_event = Event()
stop_event = Event()
logs = deque(maxlen=500)
state = {
    "status":"idle", "task_id":None, "url":"", "done":0, "total":0,
    "error":None, "verification_label":None
}
worker = None

class StartRequest(BaseModel):
    url: str
    max_listings: int = Field(default=50, ge=1, le=500)
    scroll_pause: float = Field(default=3.5, ge=1, le=30)

def log(message):
    with lock:
        logs.append(str(message))
    print(message, flush=True)

def valid_dubizzle_url(value):
    p=urlparse(value.strip())
    return p.scheme in ("http","https") and p.hostname and (
        p.hostname == "dubizzle.com.eg" or p.hostname.endswith(".dubizzle.com.eg")
    )

def verification_wait(label):
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        with lock:
            state["status"] = "waiting_for_human_verification"
            state["verification_label"] = label
        log("[VERIFICATION] A human check appeared. This cloud build cannot display its remote browser; the task is paused and may time out.")
        return
    with lock:
        state["status"]="waiting_for_human_verification"
        state["verification_label"]=label
    log(f"[VERIFICATION] Human verification required on the opened Chromium window: {label}")
    log("[VERIFICATION] Solve it manually in that same browser, then press Continue here.")
    # The scraper thread remains blocked inside this callback; its Playwright
    # context/page remain alive in the same thread and are not recreated.
    resume_event.wait()
    resume_event.clear()
    with lock:
        if state["status"] == "stopping":
            return
        state["status"]="running"
        state["verification_label"]=None
    log("[VERIFICATION] Continue requested. Returning to the same Playwright task.")
    # The scraper's own wait_for_human_check performs a page load wait after this.

def progress(done,total):
    with lock:
        state["done"]=done
        state["total"]=total

def run_job(req):
    global worker
    try:
        with lock:
            state.update(status="running", url=req.url, done=0, total=0, error=None)
        log(f"[SEARCH] Opening exact URL: {req.url}")
        scraper.run_scrape(
            req.url, log=log, on_verification_wait=verification_wait,
            should_stop=stop_event.is_set, open_report=False,
            max_listings=req.max_listings,
            scroll_pause_seconds=req.scroll_pause,
            on_progress=progress,
            on_result=lambda row: log(f"[RESULT] Saved {row.get('classification','unknown')}: {row.get('listing_url','')}")
        )
        with lock:
            state["status"]="stopped" if stop_event.is_set() else "completed"
        log("[TASK] Scraping task finished.")
    except Exception as exc:
        with lock:
            state.update(status="error", error=f"{type(exc).__name__}: {exc}")
        log("[ERROR] "+traceback.format_exc())
    finally:
        stop_event.clear()
        resume_event.set()

@app.get("/")
def home():
    return FileResponse(ROOT/"static"/"index.html")

@app.get("/api/status")
def get_status():
    with lock:
        result=dict(state)
        result["logs"]=list(logs)[-100:]
    result["owners"]=scraper.read_owner_rows()
    result["all_records"]=scraper.read_log_rows()
    return result

@app.post("/api/start")
def start(req: StartRequest):
    global worker
    if not valid_dubizzle_url(req.url):
        raise HTTPException(400,"Enter a valid https://www.dubizzle.com.eg search-results URL.")
    with lock:
        if worker and worker.is_alive():
            raise HTTPException(409,"A scraping task is already active. Resume or stop that task first.")
        state.update(status="starting", task_id=os.urandom(6).hex(), url=req.url,
                     done=0,total=0,error=None,verification_label=None)
        logs.clear()
    stop_event.clear(); resume_event.clear()
    worker=Thread(target=run_job,args=(req,),daemon=True)
    worker.start()
    return {"ok":True,"task_id":state["task_id"]}

@app.post("/api/resume")
def resume():
    with lock:
        if state["status"] != "waiting_for_human_verification":
            raise HTTPException(409,"There is no task waiting for human verification.")
    resume_event.set()
    return {"ok":True,"message":"Resume signal sent to the existing task."}

@app.post("/api/stop")
def stop():
    stop_event.set()
    resume_event.set()
    with lock:
        if state["status"] in ("running","waiting_for_human_verification","starting"):
            state["status"]="stopping"
    log("[TASK] Stop requested; the scraper will stop at its next safe checkpoint.")
    return {"ok":True}

@app.post("/api/login")
def login():
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        raise HTTPException(
            501,
            "Cloud browser login is not available in this build. A remote-browser/manual-verification flow must be configured first."
        )
    global worker
    with lock:
        if worker and worker.is_alive():
            raise HTTPException(409,"Stop the current task before opening login.")
        state.update(status="login_open",error=None)
    done=Event()
    def login_job():
        try:
            log("[LOGIN] Opening persistent Dubizzle browser. Log in and set filters.")
            scraper.run_login_only(log=log, wait_for_done=login_done_event.wait)
            log("[LOGIN] Browser session saved.")
            with lock: state["status"]="idle"
        except Exception as e:
            with lock: state.update(status="error",error=str(e))
            log(f"[LOGIN ERROR] {e}")
    worker=Thread(target=login_job,daemon=True); worker.start()
    return {"ok":True,"message":"Browser opened. Complete login, then click Done Login."}

@app.post("/api/login-done")
def login_done():
    # Login event is attached to the active login thread through module state below.
    if not login_done_event.is_set():
        login_done_event.set()
    return {"ok":True}

login_done_event=Event()

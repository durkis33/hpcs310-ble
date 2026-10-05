#!/usr/bin/env python3
import argparse
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
CONFIG_PATH = ROOT / "config.json"
JOBS = {}
LOCK = threading.Lock()

def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("Missing config.json. Run install.ps1 first.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

CONFIG = load_config()

def audit(event):
    path = ROOT / CONFIG.get("audit_log", "logs/audit.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

def auth_ok(headers):
    token = CONFIG.get("auth_token", "")
    supplied = headers.get("Authorization", "")
    return bool(token) and secrets.compare_digest(supplied, f"Bearer {token}")

def allowed(command):
    for pat in CONFIG.get("deny_patterns", []):
        if re.search(pat, command):
            return False, "denied_by_policy"
    allow = CONFIG.get("allow_patterns", [])
    if allow and not any(re.search(pat, command) for pat in allow):
        return False, "not_allowlisted"
    return True, None

def run_job(job_id, argv, cwd, timeout_s, display):
    started = time.time()
    with LOCK:
        JOBS[job_id]["status"] = "running"
    try:
        cp = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s
        )
        result = {
            "status": "completed",
            "stdout": cp.stdout,
            "stderr": cp.stderr,
            "exit_code": cp.returncode,
        }
    except subprocess.TimeoutExpired as e:
        result = {
            "status": "timed_out",
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "exit_code": None,
        }
    except Exception as e:
        result = {"status":"failed","stdout":"","stderr":str(e),"exit_code":None}
    result.update({
        "job_id": job_id,
        "duration_seconds": round(time.time() - started, 3),
        "working_directory": str(cwd),
        "command": display,
        "service_version": VERSION,
    })
    with LOCK:
        JOBS[job_id].update(result)
    audit({"event":"job_finished", **result})

def launch(argv, cwd, timeout_s, display):
    job_id = str(uuid.uuid4())
    rec = {
        "job_id": job_id,
        "status": "queued",
        "working_directory": str(cwd),
        "command": display,
        "service_version": VERSION,
    }
    with LOCK:
        JOBS[job_id] = rec
    audit({"event":"job_created", **rec})
    threading.Thread(target=run_job,args=(job_id,argv,cwd,timeout_s,display),daemon=True).start()
    return rec

class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def require_auth(self):
        if not auth_ok(self.headers):
            self.send_json(401, {"error":"unauthorized"})
            return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length","0"))
        return json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            return self.send_json(200, {"ok":True,"service_version":VERSION})
        if path.startswith("/jobs/"):
            if not self.require_auth(): return
            job_id = path.split("/")[-1]
            with LOCK:
                rec = JOBS.get(job_id)
            return self.send_json(200 if rec else 404, rec or {"error":"job_not_found"})
        self.send_json(404, {"error":"not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.require_auth(): return
        try:
            body = self.read_json()
        except Exception as e:
            return self.send_json(400, {"error":"invalid_json","detail":str(e)})

        if path == "/exec":
            command = str(body.get("command","")).strip()
            ok, why = allowed(command)
            if not ok:
                return self.send_json(403, {"error":why})
            cwd = Path(body.get("cwd") or os.getcwd()).expanduser().resolve()
            timeout_s = min(int(body.get("timeout_seconds",120)), int(CONFIG.get("max_timeout_seconds",1800)))
            ps = CONFIG.get("powershell_executable","powershell.exe")
            return self.send_json(202, launch([ps,"-NoProfile","-NonInteractive","-Command",command],cwd,timeout_s,command))

        if path == "/read-file":
            p = Path(body.get("path","")).expanduser().resolve()
            try:
                return self.send_json(200, {"path":str(p),"content":p.read_text(encoding="utf-8")})
            except Exception as e:
                return self.send_json(400, {"error":"read_failed","detail":str(e)})

        if path == "/list-files":
            p = Path(body.get("path","")).expanduser().resolve()
            try:
                items=[{"name":x.name,"path":str(x),"is_dir":x.is_dir()} for x in p.iterdir()]
                return self.send_json(200, {"path":str(p),"items":items})
            except Exception as e:
                return self.send_json(400, {"error":"list_failed","detail":str(e)})

        self.send_json(404, {"error":"not_found"})

    def log_message(self, fmt, *args):
        audit({"event":"http","client":self.client_address[0],"message":fmt % args})

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--version",action="store_true")
    args=ap.parse_args()
    if args.version:
        print(VERSION); return
    host=CONFIG.get("bind_host","127.0.0.1")
    port=int(CONFIG.get("port",8765))
    if host not in ("127.0.0.1","localhost"):
        raise SystemExit("Refusing non-loopback bind. Change architecture before remote exposure.")
    audit({"event":"service_start","version":VERSION,"host":host,"port":port})
    ThreadingHTTPServer((host,port),Handler).serve_forever()

if __name__=="__main__":
    main()

"""Standalone authenticated PowerShell job service; Python 3.9+, stdlib only."""
import argparse
import base64
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.0.0'
TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'denied'}


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def text(value, name):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise APIError(400, name + ' must be a nonempty string without NUL')
    return value


class Service:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.jobs = {}
        self.stopping = False
        self.roots = [Path(p).resolve(strict=True) for p in config['allowed_roots']]
        if not self.roots or any(not p.is_dir() for p in self.roots):
            raise ValueError('allowed_roots must contain existing directories')
        self.token = Path(config['token_file']).read_text().strip()
        self.admin = Path(config['approval_token_file']).read_text().strip()
        if min(len(self.token), len(self.admin)) < 32 or self.token == self.admin:
            raise ValueError('Use separate random credentials of at least 32 characters')
        self.audit_path = Path(config['audit_file']).resolve()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.shell = shutil.which(config.get('powershell', 'powershell.exe'))
        if not self.shell:
            raise ValueError('PowerShell executable not found')
        self.mode = config.get('execution_policy', 'confirm')
        if self.mode not in ('confirm', 'allow', 'deny'):
            raise ValueError('execution_policy must be confirm, allow or deny')
        self.deny = [re.compile(p, re.I) for p in config.get('deny_patterns', [])]
        self.confirm = [re.compile(p, re.I) for p in config.get('confirm_patterns', [])]
        for name, default in [('max_timeout', 300), ('max_jobs', 100), ('max_running', 4),
                              ('max_output_bytes', 1048576), ('max_file_bytes', 1048576),
                              ('max_snapshot_files', 5000)]:
            value = config.get(name, default)
            if type(value) is not int or value < 1:
                raise ValueError(name + ' must be a positive integer')
            setattr(self, name, value)
        self.audit('service_started', version=VERSION)

    def audit(self, event, **fields):
        # Append only, flushed before execution is permitted. Never logs credentials.
        with self.lock, self.audit_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(timestamp=time.time(), event=event, **fields), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def path(self, value, directory=False):
        p = Path(text(value, 'path'))
        if not p.is_absolute():
            raise APIError(400, 'An absolute path is required')
        try:
            p = p.resolve(strict=True)
        except OSError:
            raise APIError(404, 'Path not found')
        if not any(p == r or r in p.parents for r in self.roots):
            raise APIError(403, 'Path is outside allowed_roots')
        protected = [Path(self.config[k]).resolve() for k in ('token_file', 'approval_token_file', 'audit_file')]
        if p in protected:
            raise APIError(403, 'Service state is not available through file tools')
        if directory and not p.is_dir():
            raise APIError(400, 'Working directory must be a directory')
        return p

    def public(self, job):
        return copy.deepcopy({k: v for k, v in job.items() if not k.startswith('_')})

    def script_hash(self, path):
        with Path(path).open('rb') as f:
            data = f.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise APIError(413, 'Script exceeds max_file_bytes')
        return hashlib.sha256(data).hexdigest()

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise APIError(404, 'Unknown job')
            return self.public(self.jobs[job_id])

    def submit(self, request, script=False):
        allowed = {'path', 'args', 'cwd', 'timeout'} if script else {'command', 'cwd', 'timeout'}
        if set(request) - allowed:
            raise APIError(400, 'Unknown execution fields')
        cwd = self.path(request.get('cwd'), directory=True)
        timeout = request.get('timeout', min(60, self.max_timeout))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= self.max_timeout:
            raise APIError(400, 'timeout must be positive and at most ' + str(self.max_timeout))
        invoked = {'cwd': str(cwd), 'timeout': timeout}
        if script:
            p = self.path(request.get('path'))
            if p.suffix.lower() != '.ps1' or not p.is_file():
                raise APIError(400, 'exec_script requires a .ps1 file')
            args = request.get('args', [])
            if not isinstance(args, list) or any(not isinstance(a, str) or '\x00' in a for a in args):
                raise APIError(400, 'args must be an array of strings without NUL')
            invoked.update(path=str(p), args=args)
            invoked['sha256'] = self.script_hash(p)
        else:
            invoked['command'] = text(request.get('command'), 'command')
        policy_text = json.dumps(invoked)
        if self.mode == 'deny' or any(p.search(policy_text) for p in self.deny):
            self.audit('execution_denied', invoked=invoked)
            raise APIError(403, 'Execution denied by policy')
        with self.lock:
            if self.stopping:
                raise APIError(503, 'Service is stopping')
            automatic = self.mode == 'allow' and not any(p.search(policy_text) for p in self.confirm)
            if automatic and sum(j['state'] == 'running' for j in self.jobs.values()) >= self.max_running:
                raise APIError(429, 'Running job capacity reached')
            if len(self.jobs) >= self.max_jobs:
                oldest = next((k for k, v in self.jobs.items() if v['state'] in TERMINAL), None)
                if oldest:
                    del self.jobs[oldest]
                else:
                    raise APIError(429, 'Job capacity reached')
            ident = str(uuid.uuid4())
            job = dict(job_id=ident, audit_id=ident, service_version=VERSION,
                       state='pending_approval', stdout='', stderr='', exit_code=None,
                       duration=0, working_directory=str(cwd), invoked=invoked,
                       changed_files=[], snapshot_truncated=False, output_truncated=False,
                       _cancel=threading.Event())
            self.audit('job_submitted', job_id=ident, invoked=invoked)
            self.jobs[ident] = job
            if automatic:
                self._start(job)
            return self.public(job)

    def _start(self, job):
        if sum(j['state'] == 'running' for j in self.jobs.values()) >= self.max_running:
            raise APIError(429, 'Running job capacity reached; approve this job later')
        self.audit('job_started', job_id=job['job_id'])
        job['state'] = 'running'
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        job['_thread'] = thread
        thread.start()

    def approve(self, ident):
        with self.lock:
            if self.stopping:
                raise APIError(503, 'Service is stopping')
            self.get(ident)
            job = self.jobs[ident]
            if job['state'] != 'pending_approval':
                raise APIError(409, 'Job is not pending approval')
            self.audit('job_approved', job_id=ident)
            self._start(job)
            return self.public(job)

    def cancel(self, ident):
        with self.lock:
            self.get(ident)
            job = self.jobs[ident]
            self.audit('cancel_requested', job_id=ident)
            job['_cancel'].set()
            if job['state'] == 'pending_approval':
                job['state'] = 'cancelled'
            return self.public(job)

    def snapshot(self, cwd):
        result = {}
        count = 0
        pending = [str(cwd)]
        while pending:
            try:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        count += 1
                        if count > self.max_snapshot_files:
                            return result, True
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            if getattr(stat, 'st_file_attributes', 0) & 0x400:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in ('.git', '.venv', '__pycache__'):
                                    pending.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                result[entry.path] = (stat.st_mtime_ns, stat.st_size)
                        except OSError:
                            pass
            except OSError:
                pass
        return result, False

    def _run(self, job):
        start = time.monotonic()
        process = tree = None
        readers = []
        buffers = [bytearray(), bytearray()]
        state = 'failed'
        before = {}
        try:
            inv = job['invoked']
            before, truncated = self.snapshot(inv['cwd'])
            job['snapshot_truncated'] = truncated
            if 'path' in inv and self.script_hash(inv['path']) != inv['sha256']:
                raise RuntimeError('Script changed since submission; submit again for approval')
            if job['_cancel'].is_set():
                state = 'cancelled'
                return
            if time.monotonic() - start >= inv['timeout']:
                state = 'timed_out'
                return
            payload = base64.b64encode(json.dumps(inv, ensure_ascii=False).encode('utf-8')).decode()
            wrapper = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
if ([Console]::ReadLine() -ne 'go') { exit 125 }
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
$global:LASTEXITCODE = 0
try {
  if ($p.PSObject.Properties.Name -contains 'path') {
    $scriptArgs = @($p.args)
    & $p.path @scriptArgs
  } else { & ([ScriptBlock]::Create($p.command)) }
  if (-not $?) { exit 1 }
  exit $global:LASTEXITCODE
} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }
""".replace('PAYLOAD', payload)
            encoded = base64.b64encode(wrapper.encode('utf-16le')).decode()
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            process = subprocess.Popen([self.shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                       cwd=inv['cwd'], env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if os.name == 'nt':
                from winjob import Job
                tree = Job(process)

            def drain(stream, buffer):
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    room = self.max_output_bytes - len(buffer)
                    buffer.extend(chunk[:max(0, room)])
                    if len(chunk) > room:
                        job['output_truncated'] = True
                stream.close()

            for stream, buffer in zip((process.stdout, process.stderr), buffers):
                t = threading.Thread(target=drain, args=(stream, buffer), daemon=True)
                readers.append(t)
                t.start()
            if job['_cancel'].is_set():
                state = 'cancelled'
                return
            process.stdin.write(b'go\n')
            process.stdin.close()
            while process.poll() is None:
                if job['_cancel'].wait(0.03):
                    state = 'cancelled'
                    break
                if time.monotonic() - start >= inv['timeout']:
                    state = 'timed_out'
                    break
            else:
                state = 'completed' if process.returncode == 0 else 'failed'
        except Exception as exc:
            buffers[1].extend(str(exc).encode('utf-8')[:self.max_output_bytes])
        finally:
            if tree:
                tree.close()  # Also kills descendants left after a successful parent exit.
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()
                job['exit_code'] = process.returncode
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                for t in readers:
                    t.join(timeout=5)
            after, truncated = self.snapshot(job['working_directory'])
            with self.lock:
                job.update(stdout=buffers[0].decode('utf-8', errors='replace'),
                           stderr=buffers[1].decode('utf-8', errors='replace'),
                           duration=round(time.monotonic() - start, 3),
                           changed_files=[p for p, stat in after.items() if before.get(p) != stat],
                           snapshot_truncated=job['snapshot_truncated'] or truncated)
                try:
                    record = self.public(job)
                    record['state'] = state
                    self.audit('job_finished', **record)
                except OSError:
                    state = 'failed'
                    job['stderr'] += '\nAudit write failed'
                job['state'] = state

    def shutdown(self):
        with self.lock:
            self.stopping = True
            jobs = list(self.jobs.values())
            for job in jobs:
                job['_cancel'].set()
        for job in jobs:
            if '_thread' in job:
                job['_thread'].join(timeout=10)
        self.audit('service_stopped')

    def files(self, request, listing=False):
        if set(request) != {'path'}:
            raise APIError(400, 'Expected path only')
        p = self.path(request.get('path'))
        self.audit('list_files' if listing else 'read_file', path=str(p))
        if listing:
            if not p.is_dir():
                raise APIError(400, 'Expected a directory')
            entries = []
            for item in p.iterdir():
                if len(entries) >= self.max_snapshot_files:
                    return dict(entries=entries, truncated=True)
                entries.append(dict(name=item.name, is_directory=item.is_dir()))
            return dict(entries=entries, truncated=False)
        if not p.is_file():
            raise APIError(400, 'Expected a file')
        with p.open('rb') as f:
            data = f.read(self.max_file_bytes + 1)
        truncated = len(data) > self.max_file_bytes
        return dict(path=str(p), encoding='base64', content=base64.b64encode(data[:self.max_file_bytes]).decode(), truncated=truncated)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, service):
        self.service = service
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_GET(self):
        self.respond(405, dict(ok=False, request_id=str(uuid.uuid4()), error='Use POST with JSON'))

    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_OPTIONS = do_GET

    def do_POST(self):
        service = self.server.service
        request_id = str(uuid.uuid4())
        try:
            approval = self.path in ('/approve', '/shutdown')
            secret = service.admin if approval else service.token
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + secret).encode()):
                service.audit('authentication_failed', request_id=request_id, route=self.path)
                raise APIError(401, 'Authentication required')
            if approval and self.client_address[0] not in ('127.0.0.1', '::1'):
                raise APIError(403, 'Administration is local only')
            if self.headers.get_content_type() != 'application/json':
                raise APIError(415, 'Use application/json')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536 or self.headers.get('Transfer-Encoding'):
                raise APIError(413, 'Request body must be 1..65536 bytes')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise APIError(400, 'Expected a JSON object')
            service.audit('request', request_id=request_id, route=self.path)
            if self.path == '/status':
                result = dict(service_version=VERSION, execution_policy=service.mode,
                              platform=os.name, stopping=service.stopping)
            elif self.path in ('/exec', '/exec_script'):
                result = service.submit(body, self.path == '/exec_script')
            elif self.path in ('/get_job', '/cancel', '/approve'):
                if set(body) != {'job_id'}:
                    raise APIError(400, 'Expected job_id only')
                ident = text(body.get('job_id'), 'job_id')
                result = getattr(service, {'/get_job': 'get', '/cancel': 'cancel', '/approve': 'approve'}[self.path])(ident)
            elif self.path in ('/read_file', '/list_files'):
                result = service.files(body, self.path == '/list_files')
            elif self.path == '/shutdown':
                result = dict(stopping=True)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                raise APIError(404, 'Unknown endpoint')
            self.respond(200, dict(ok=True, request_id=request_id, result=result))
        except APIError as exc:
            self.respond(exc.status, dict(ok=False, request_id=request_id, error=exc.message))
        except (ValueError, TypeError, UnicodeError):
            self.respond(400, dict(ok=False, request_id=request_id, error='Invalid request'))
        except Exception:
            self.respond(500, dict(ok=False, request_id=request_id, error='Internal service error'))

    def respond(self, status, body):
        data = json.dumps(dict(service_version=VERSION, **body), ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    # Keep the HTTP transport on loopback. Terminate HTTPS at a separately managed gateway.
    if config.get('host', '127.0.0.1') != '127.0.0.1':
        parser.error('host must be 127.0.0.1; use an authenticated HTTPS gateway')
    if os.name != 'nt':
        parser.error('This release requires Windows for process-tree containment')
    service = Service(config)
    server = Server(('127.0.0.1', config.get('port', 8765)), service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.shutdown()


if __name__ == '__main__':
    main()

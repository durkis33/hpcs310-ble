"""Native Windows lifecycle commands for the local PowerShell service.

This module deliberately uses Python and cmd.exe launchers. PowerShell scripts
are not used for installation, startup, shutdown, restart, or approval.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


SERVICE_VERSION = '1.0.0'


def fail(message):
    raise RuntimeError(message)


def command(args):
    completed = subprocess.run(args, text=True, capture_output=True, check=False)
    if completed.returncode:
        fail('%s failed (%s):\n%s%s' % (' '.join(map(str, args)), completed.returncode,
             completed.stdout, completed.stderr))
    return completed.stdout


def user_sid():
    # schtasks requires a concrete account for an InteractiveToken task.
    output = command(['whoami.exe', '/user', '/fo', 'csv', '/nh']).strip()
    try:
        row = next(__import__('csv').reader([output]))
        return row[1]
    except (IndexError, StopIteration):
        fail('Could not determine the current Windows user SID')


def protect(path, sid):
    command(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
             '*%s:(OI)(CI)F' % sid, '*S-1-5-18:(OI)(CI)F'])


def load_config(path):
    return json.loads((path / 'config.json').read_text(encoding='utf-8-sig'))


def read_secret(config, name):
    return Path(config[name]).read_text(encoding='utf-8').strip()


def startup_launcher(destination):
    return '@echo off\r\ncall "{}" host\r\n'.format(destination / 'Run.cmd')


def endpoint(config, route):
    return 'http://127.0.0.1:%d%s' % (config['port'], route)


def request(config, route, secret_name, body):
    data = json.dumps(body).encode('utf-8')
    call = urllib.request.Request(endpoint(config, route), data=data,
        headers={'Authorization': 'Bearer ' + read_secret(config, secret_name),
                 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(call, timeout=5) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        answer = json.load(error)
    if not answer.get('ok'):
        fail('Service request %s failed: %s' % (route, answer.get('error', 'unknown error')))
    return answer['result']


def wait_for_status(config, want_running, seconds=20):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            status = request(config, '/status', 'token_file', {})
            if want_running:
                return status
        except (OSError, ValueError, urllib.error.URLError, RuntimeError) as error:
            last_error = error
            if not want_running:
                return None
        time.sleep(.25)
    if want_running:
        fail('Service did not become ready within %s seconds: %s' % (seconds, last_error or 'no response'))
    fail('Service did not stop within %s seconds' % seconds)


def install(args):
    source = Path(__file__).resolve().parent
    python = Path(args.python).resolve(strict=True)
    if not python.is_file():
        fail('Python executable not found: %s' % python)
    command([str(python), '--version'])
    roots = [Path(value).resolve(strict=True) for value in args.allowed_root]
    if any(not root.is_dir() for root in roots):
        fail('Every allowed root must be an existing directory')
    destination = Path(args.destination or Path(os.environ['LOCALAPPDATA']) / 'PowerShellService')
    if destination.exists():
        fail('Destination already exists: %s. Preserve its state and upgrade source files deliberately.' % destination)
    sid = user_sid()
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'state', 'config.json', 'python.txt'))
    protect(destination, sid)
    state = destination / 'state'
    state.mkdir()
    for name in ('api.token', 'approval.token'):
        (state / name).write_text(base64.b64encode(secrets.token_bytes(32)).decode('ascii'), encoding='utf-8')
    config = json.loads((destination / 'config.example.json').read_text(encoding='utf-8-sig'))
    config.update(allowed_roots=[str(root) for root in roots],
                  token_file=str(state / 'api.token'), approval_token_file=str(state / 'approval.token'),
                  audit_file=str(state / 'audit.jsonl'))
    (destination / 'config.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    (destination / 'python.txt').write_text(str(python) + '\n', encoding='utf-8')
    startup = Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / 'GeneralPowerShellService.cmd'
    startup.write_text(startup_launcher(destination), encoding='ascii')
    print('Installed %s. Starting it through the native user launcher…' % destination)
    start(argparse.Namespace(path=destination))


def host(args):
    path = Path(args.path).resolve()
    config = load_config(path)
    try:
        status = wait_for_status(config, True, seconds=1)
        print(json.dumps(dict(message='Service is already running', status=status), indent=2))
        return
    except RuntimeError:
        pass
    python = Path((path / 'python.txt').read_text(encoding='utf-8').strip()).resolve(strict=True)
    log = (path / 'state' / 'service.stderr.log').open('ab', buffering=0)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([str(python), str(path / 'service.py'), '--config', str(path / 'config.json')],
                     cwd=path, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     creationflags=flags, close_fds=True)
    log.close()
    status = wait_for_status(config, True)
    print(json.dumps(dict(message='Service started', launcher='Startup folder', status=status), indent=2))


def start(args):
    host(args)


def stop(args):
    path = Path(args.path).resolve()
    config = load_config(path)
    try:
        request(config, '/shutdown', 'approval_token_file', {})
    except (OSError, ValueError, urllib.error.URLError, RuntimeError) as error:
        # A task that is already stopped is an idempotent successful stop.
        if 'Connection refused' not in str(error) and 'timed out' not in str(error):
            raise
    wait_for_status(config, False)
    print(json.dumps(dict(message='Service stopped', launcher='Startup folder'), indent=2))


def restart(args):
    stop(args)
    start(args)


def approve(args):
    path = Path(args.path).resolve()
    config = load_config(path)
    job = request(config, '/get_job', 'token_file', {'job_id': args.job_id})
    print(json.dumps(job, indent=2, ensure_ascii=False))
    if input('Review the exact invocation above. Type APPROVE to run it: ') != 'APPROVE':
        fail('Not approved')
    result = request(config, '/approve', 'approval_token_file', {'job_id': args.job_id})
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=SERVICE_VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    installer = commands.add_parser('install')
    installer.add_argument('--python', required=True)
    installer.add_argument('--allowed-root', action='append', required=True)
    installer.add_argument('--destination')
    for name in ('start', 'stop', 'restart', 'host'):
        item = commands.add_parser(name)
        item.add_argument('--path', default=Path(__file__).resolve().parent)
    approval = commands.add_parser('approve')
    approval.add_argument('job_id')
    approval.add_argument('--path', default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    globals()[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print('ERROR: %s' % error, file=sys.stderr)
        raise SystemExit(1)

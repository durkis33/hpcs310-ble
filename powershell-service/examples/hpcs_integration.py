"""First project integration; the service itself has no HPCS dependencies.

Run against an installed service. Approve each job separately in a local terminal.
"""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client import call


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', required=True)
    p.add_argument('--token-file', required=True)
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--python', default='py', help='Python executable; defaults to the Windows launcher')
    p.add_argument('--hardware', action='store_true', help='Also acquire flicker data from a connected spectrometer')
    a = p.parse_args()
    token = Path(a.token_file).read_text().strip()
    runner = "& '" + a.python.replace("'", "''") + "'"
    commands = [runner + ' hpcs310.py selftest']
    if a.hardware:
        commands.append(runner + ' hpcs310.py flicker --auto -v')
    for command in commands:
        response = call(a.url, token, 'exec', dict(command=command, cwd=str(Path(a.repo).resolve()), timeout=120))
        if not response['ok']:
            raise RuntimeError(response)
        job = response['result']
        print('Job:', job['job_id'], '- approve locally if pending_approval', flush=True)
        deadline = time.monotonic() + 600
        while job['state'] in ('pending_approval', 'running'):
            if time.monotonic() >= deadline:
                call(a.url, token, 'cancel', {'job_id': job['job_id']})
                raise TimeoutError('Integration wait exceeded 10 minutes; job cancelled')
            time.sleep(.5)
            response = call(a.url, token, 'get_job', {'job_id': job['job_id']})
            if not response['ok']:
                raise RuntimeError(response)
            job = response['result']
        print(json.dumps(job, ensure_ascii=False, indent=2))
        evidence = sorted({str(Path(f).parent) for f in job['changed_files'] if Path(f).name == 'flicker.json'})
        print('Evidence directories:', json.dumps(evidence))
        if job['exit_code'] != 0 or job['state'] != 'completed':
            raise SystemExit(1)


if __name__ == '__main__':
    main()

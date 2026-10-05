import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import APIError, Service, Server


@unittest.skipUnless(os.name == 'nt', 'Windows process containment required')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        # Test-process scope only; never changes the machine/user execution policy.
        self.old_policy = os.environ.get('PSExecutionPolicyPreference')
        os.environ['PSExecutionPolicyPreference'] = 'RemoteSigned'
        self.tmp = tempfile.TemporaryDirectory(prefix='service test ')
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        for name, token in [('api', 'a' * 40), ('admin', 'b' * 40)]:
            (self.root / name).write_text(token)
        self.s = Service(dict(allowed_roots=[str(self.project)], token_file=str(self.root / 'api'),
            approval_token_file=str(self.root / 'admin'), audit_file=str(self.root / 'audit.jsonl'),
            powershell=shutil.which('powershell.exe'), max_output_bytes=4096, max_timeout=30))

    def tearDown(self):
        self.s.shutdown()
        self.tmp.cleanup()
        if self.old_policy is None:
            os.environ.pop('PSExecutionPolicyPreference', None)
        else:
            os.environ['PSExecutionPolicyPreference'] = self.old_policy

    def submit(self, command, timeout=20):
        return self.s.submit(dict(command=command, cwd=str(self.project), timeout=timeout))

    def finish(self, job):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            result = self.s.get(job['job_id'])
            if result['state'] not in ('running', 'pending_approval'):
                return result
            time.sleep(.05)
        self.fail('Job did not finish')

    def run_job(self, command, timeout=20):
        job = self.submit(command, timeout)
        self.s.approve(job['job_id'])
        return self.finish(job)

    def test_approval_unicode_cwd_and_files(self):
        job = self.submit("[Console]::WriteLine('héllo 世界'); [IO.File]::WriteAllText((Join-Path (Get-Location) 'evidence.txt'), 'ok')")
        self.assertEqual(job['state'], 'pending_approval')
        self.assertFalse((self.project / 'evidence.txt').exists())
        self.s.approve(job['job_id'])
        result = self.finish(job)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('héllo 世界', result['stdout'])
        self.assertIn(str(self.project / 'evidence.txt'), result['changed_files'])
        events = [json.loads(line)['event'] for line in (self.root / 'audit.jsonl').read_text().splitlines()]
        self.assertIn('job_approved', events)
        self.assertIn('job_finished', events)

    def test_script_arguments_are_data(self):
        script = self.project / "a ' quoted script.ps1"
        script.write_text('[Console]::WriteLine(($args | ConvertTo-Json -Compress))', encoding='utf-8-sig')
        args = ['space value', "quote'\"", '; throw 123', '$env:PATH', '世界']
        job = self.s.submit(dict(path=str(script), args=args, cwd=str(self.project)), script=True)
        self.s.approve(job['job_id'])
        result = self.finish(job)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(json.loads(result['stdout']), args)

    def test_failure_timeout_and_cancel(self):
        self.assertEqual(self.run_job('exit 17')['exit_code'], 17)
        self.assertEqual(self.run_job("Write-Error 'bad'")['state'], 'failed')
        self.assertEqual(self.run_job('Start-Sleep 20', timeout=1)['state'], 'timed_out')
        job = self.submit('Start-Sleep 20')
        self.s.approve(job['job_id'])
        self.s.cancel(job['job_id'])
        self.assertEqual(self.finish(job)['state'], 'cancelled')

    def test_output_bound_and_validation(self):
        result = self.run_job("[Console]::Write('x' * 20000)")
        self.assertEqual(len(result['stdout']), 4096)
        self.assertTrue(result['output_truncated'])
        for timeout in [0, -1, 31, float('nan'), True]:
            with self.assertRaises(APIError):
                self.submit('echo ok', timeout)
        with self.assertRaises(APIError):
            self.s.path(str(self.root))
        with self.assertRaises(APIError):
            self.s.path('relative')

    def test_script_change_rejected(self):
        script = self.project / 'change.ps1'
        script.write_text('echo before')
        job = self.s.submit(dict(path=str(script), cwd=str(self.project)), script=True)
        script.write_text('echo after')
        self.s.approve(job['job_id'])
        result = self.finish(job)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('changed since submission', result['stderr'])

    def test_http_auth_approval_and_errors(self):
        server = Server(('127.0.0.1', 0), self.s)
        t = threading.Thread(target=server.serve_forever)
        t.start()
        def request(route, token, body):
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (server.server_port, route),
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)
        try:
            self.assertEqual(request('/status', 'wrong', {})[0], 401)
            self.assertEqual(request('/status', 'a' * 40, {})[0], 200)
            job = self.submit('echo ok')
            self.assertEqual(request('/approve', 'a' * 40, {'job_id': job['job_id']})[0], 401)
            self.assertEqual(request('/approve', 'b' * 40, {'job_id': job['job_id']})[0], 200)
            self.assertEqual(self.finish(job)['exit_code'], 0)
            self.assertEqual(request('/exec', 'a' * 40, {'command': 'echo ok'})[0], 400)
            self.assertEqual(request('/get_job', 'a' * 40, {'job_id': 'missing'})[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            t.join()

    def test_descendant_is_killed_after_parent_exits(self):
        import base64
        child = "Start-Sleep 3; [IO.File]::WriteAllText('" + str(self.project / 'escaped.txt').replace("'", "''") + "', 'bad')"
        encoded = base64.b64encode(child.encode('utf-16le')).decode()
        command = "Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile -EncodedCommand " + encoded + "'"
        result = self.run_job(command)
        self.assertEqual(result['state'], 'completed', result)
        time.sleep(3.5)
        self.assertFalse((self.project / 'escaped.txt').exists())

    def test_policy_pending_cancel_and_file_tools(self):
        import base64
        import re
        self.s.deny = [re.compile('forbidden', re.I)]
        with self.assertRaises(APIError):
            self.submit('echo forbidden')
        job = self.submit('echo ok')
        self.s.cancel(job['job_id'])
        with self.assertRaises(APIError):
            self.s.approve(job['job_id'])
        p = self.project / 'binary.bin'
        p.write_bytes(b'\x00\xffhello')
        result = self.s.files({'path': str(p)})
        self.assertEqual(base64.b64decode(result['content']), p.read_bytes())
        self.assertEqual(self.s.files({'path': str(self.project)}, True)['entries'][0]['name'], 'binary.bin')

    def test_hpcs_first_integration(self):
        repo = Path(__file__).resolve().parents[2]
        if not (repo / 'hpcs310.py').exists():
            self.skipTest('Optional HPCS integration requires the original repository')
        self.s.roots.append(repo)
        command = "& '" + sys.executable.replace("'", "''") + "' hpcs310.py selftest"
        job = self.s.submit(dict(command=command, cwd=str(repo), timeout=30))
        self.s.approve(job['job_id'])
        result = self.finish(job)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('PASS', result['stdout'])

    def test_automatic_capacity_has_no_orphan_jobs(self):
        self.s.mode = 'allow'
        self.s.max_running = 1
        job = self.submit('Start-Sleep 20')
        with self.assertRaises(APIError):
            self.submit('echo should-not-run')
        self.assertEqual(len(self.s.jobs), 1)
        self.s.cancel(job['job_id'])
        self.finish(job)

    def test_audit_failure_blocks_launch(self):
        from unittest.mock import patch
        job = self.submit("[IO.File]::WriteAllText('should-not-exist', 'bad')")
        with patch.object(self.s, 'audit', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.s.approve(job['job_id'])
        self.assertEqual(self.s.get(job['job_id'])['state'], 'pending_approval')
        self.assertFalse((self.project / 'should-not-exist').exists())

    def test_snapshot_and_script_read_are_bounded(self):
        for i in range(5):
            (self.project / str(i)).mkdir()
        self.s.max_snapshot_files = 2
        self.assertTrue(self.s.snapshot(self.project)[1])
        script = self.project / 'large.ps1'
        script.write_text('x' * 100)
        self.s.max_file_bytes = 20
        with self.assertRaises(APIError):
            self.s.submit(dict(path=str(script), cwd=str(self.project)), script=True)


if __name__ == '__main__':
    unittest.main()

"""Generic local/HTTPS connector. No OpenAI SDK or Codex dependency."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request


def call(base_url, token, tool, arguments):
    if not (base_url.startswith('https://') or base_url.startswith('http://127.0.0.1:')):
        raise ValueError('Use HTTPS except on loopback')
    request = urllib.request.Request(base_url.rstrip('/') + '/' + tool,
        data=json.dumps(arguments).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return json.load(exc)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('tool', choices=['status', 'exec', 'exec_script', 'cancel', 'get_job', 'read_file', 'list_files'])
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--token-file', required=True)
    p.add_argument('--request', help='UTF-8 JSON file containing tool arguments')
    a = p.parse_args()
    result = call(a.url, Path(a.token_file).read_text().strip(), a.tool,
                  json.loads(Path(a.request).read_text(encoding='utf-8-sig')) if a.request else {})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)

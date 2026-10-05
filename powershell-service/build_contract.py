"""Regenerate the checked-in GPT Actions OpenAPI contract (stdlib only)."""
import json
from pathlib import Path

string = {'type': 'string'}
cwd = dict(type='string', description='Existing absolute Windows working directory within allowed_roots.')
timeout = dict(type='number', exclusiveMinimum=0, description='Timeout in seconds, bounded by server configuration.')
specs = {
    'status': ({}, [], 'Get service version and execution policy.'),
    'exec': ({'command': string, 'cwd': cwd, 'timeout': timeout}, ['command', 'cwd'], 'Submit a PowerShell command. Poll get_job; pending_approval requires local human approval.'),
    'exec_script': ({'path': string, 'args': {'type': 'array', 'items': string}, 'cwd': cwd, 'timeout': timeout}, ['path', 'cwd'], 'Submit an existing absolute .ps1 path with positional string arguments. Poll get_job.'),
    'cancel': ({'job_id': string}, ['job_id'], 'Cancel a pending or running job and its process tree.'),
    'get_job': ({'job_id': string}, ['job_id'], 'Get state, stdout, stderr, exit_code, duration in seconds, working_directory, invoked, audit_id, service_version, changed_files and truncation flags.'),
    'read_file': ({'path': string}, ['path'], 'Read a bounded file as base64; inspect truncated before decoding.'),
    'list_files': ({'path': string}, ['path'], 'List a bounded single directory; inspect truncated.'),
}
paths = {}
for name, (properties, required, description) in specs.items():
    schema = dict(type='object', properties=properties, additionalProperties=False)
    if required:
        schema['required'] = required
    paths['/' + name] = {'post': dict(operationId=name, summary=description,
        **{'x-openai-isConsequential': name in ('exec', 'exec_script', 'cancel')},
        requestBody=dict(required=True, content={'application/json': {'schema': schema}}),
        responses={'200': {'description': 'JSON envelope. ok=true contains result; execution submission is asynchronous.',
                    'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Response'}}}},
                   'default': {'description': 'Structured error: ok=false, error, request_id, service_version.',
                    'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Response'}}}}})}
contract = dict(openapi='3.1.0', info=dict(title='General Windows PowerShell Service', version='1.0.0'),
    servers=[dict(url='https://YOUR-GATEWAY.example')], security=[{'bearerAuth': []}], paths=paths,
    components=dict(securitySchemes=dict(bearerAuth=dict(type='http', scheme='bearer')),
        schemas={'Response': dict(type='object', required=['ok', 'request_id', 'service_version'], properties={
            'ok': {'type': 'boolean'}, 'request_id': string, 'service_version': string,
            'error': string, 'result': {'type': 'object', 'additionalProperties': True}})}))
if __name__ == '__main__':
    Path(__file__).with_name('openapi.json').write_text(json.dumps(contract, indent=2) + '\n', encoding='utf-8')

import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

import yaml


REPO = os.environ['GITHUB_REPOSITORY']
assert REPO.startswith('peeweep-test/dde-release-review-20261110-'), REPO
RUN_URL = f"https://github.com/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def api(path, data=None, method=None):
    command = ['gh', 'api', path]
    if method:
        command += ['--method', method]
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, timeout=40)
    if result.returncode:
        raise RuntimeError(f'{path}: {result.stderr.strip()}')
    return json.loads(result.stdout) if result.stdout.strip() else None


def summary(text):
    print(text, flush=True)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
        output.write(text + '\n\n')


def verify_sources():
    for filename, digest in json.loads(Path('source-hashes.json').read_text()).items():
        assert hashlib.sha256(Path(filename).read_bytes()).hexdigest() == digest, filename


def controller():
    workflow = yaml.safe_load(Path('sources/sync-baseline.yml').read_text())
    step = next(s for s in workflow['jobs']['coordinate']['steps'] if s.get('id') == 'coordinate')
    code = step['run'].split("<<'PY'", 1)[1].split('\n', 1)[1].rsplit('\nPY', 1)[0]
    assert code.count('f"linuxdeepin/') == 1
    code = code.replace('f"linuxdeepin/', 'f"peeweep-test/')
    namespace = {'__name__': 'review_controller'}
    exec(compile(code, 'sources/sync-baseline.yml', 'exec'), namespace)
    return namespace


def marker(sha, name):
    api(f'repos/{REPO}/statuses/{sha}', {
        'state': 'success', 'context': name, 'description': name[:140], 'target_url': RUN_URL})


def statuses(sha):
    return api(f'repos/{REPO}/commits/{sha}/statuses?per_page=100')


def wait_marker(sha, name):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if any(s['context'] == name and s['state'] == 'success' for s in statuses(sha)):
            return
        time.sleep(3)
    raise RuntimeError(f'timed out waiting for {name}')


def race():
    namespace = controller()
    scenario, role, probe = (os.environ[k] for k in ['SCENARIO', 'ROLE', 'PROBE_ID'])
    pr = api(f"repos/{REPO}/pulls/{os.environ['BASELINE_PR']}")
    sha = pr['head']['sha']
    release = namespace['record'](REPO, sha, 'G5')
    cfg = namespace['config'](REPO, api(f'repos/{REPO}')['default_branch'])
    captured = f'review/{probe}/captured'
    proceed = f'review/{probe}/proceed'
    if role == 'older':
        original_states = namespace['states']

        def held_snapshot(*args, **kwargs):
            result = original_states(*args, **kwargs)
            expected = ['pending'] if scenario == 'pending' else ['merged']
            assert result == expected, (result, expected)
            summary(f'Older check captured real member states: `{result}`.')
            marker(sha, captured)
            wait_marker(sha, proceed)
            return result

        namespace['states'] = held_snapshot
        namespace['check'](REPO, pr, release, cfg)
        final = next(s for s in statuses(sha) if s['context'] == 'group-release-ready')
        expected = 'pending' if scenario == 'pending' else 'success'
        assert final['state'] == expected, final
        member = release['members'][0]
        live = namespace['head_version'](member['repo'], member['base'])
        summary(f'REPRODUCED: older check wrote `{final["state"]}` last on '
                f'[baseline PR #{pr["number"]}]({pr["html_url"]}). '
                f'Live member version is `{live}`, recorded target is `{member["version"]}`.')
    else:
        wait_marker(sha, captured)
        change = api(f"repos/{REPO}/pulls/{os.environ['CHANGE_PR']}")
        assert not change['merged_at'] and change['base']['ref'] == release['members'][0]['base']
        merged = api(f"repos/{REPO}/pulls/{change['number']}/merge", {
            'sha': change['head']['sha'], 'merge_method': 'merge'}, 'PUT')
        assert merged.get('merged'), merged
        summary(f'Merged real fixture [PR #{change["number"]}]({change["html_url"]}): '
                f'`{merged["sha"]}`.')
        try:
            namespace['check'](REPO, pr, release, cfg)
        except Exception as error:
            namespace['status'](REPO, sha, 'failure', str(error))
            if scenario != 'failure':
                raise
            summary(f'Fresh check correctly rejected current state: `{error}`.')
        current = next(s for s in statuses(sha) if s['context'] == 'group-release-ready')
        expected = 'success' if scenario == 'pending' else 'failure'
        assert current['state'] == expected, current
        summary(f'Fresh check published `{current["state"]}`. Releasing the older check now.')
        marker(sha, proceed)


def guard():
    workflow = yaml.safe_load(Path('sources/verify-group-version.yml').read_text())
    steps = workflow['jobs']['verify']['steps']
    pr = api(f"repos/{REPO}/pulls/{os.environ['GUARD_PR']}")
    env = {**os.environ, 'PR_HEAD_SHA': pr['head']['sha']}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        event = root / 'event.json'
        event.write_text(json.dumps({'number': pr['number']}))
        env['GITHUB_EVENT_PATH'] = str(event)
        for name in ['Mark version check pending', 'Verify changelog version']:
            script = next(s['run'] for s in steps if s['name'] == name)
            subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
                           env=env, check=True, timeout=120)
        env['VERIFY_OUTCOME'] = 'success'
        server = None
        requests = []
        if os.environ['INJECT_FAILURE'] == 'true':
            class Unavailable(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(503)
                    self.send_header('Content-Length', '0')
                    self.end_headers()

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Unavailable)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env['REVIEW_REAL_GH'] = shutil.which('gh')
            env['REVIEW_FAILURE_URL'] = f'http://127.0.0.1:{server.server_port}/unavailable'
            shim = root / 'gh'
            shim.write_text('''#!/usr/bin/env bash
if [[ "$1" == api && "$2" == "repos/$GITHUB_REPOSITORY/commits/$PR_HEAD_SHA/pulls?per_page=100" ]]; then
    echo 'TEST: redirecting final PR refresh to an empty-body HTTP 503 endpoint' >&2
    shift 2
    exec "$REVIEW_REAL_GH" api "$REVIEW_FAILURE_URL" "$@"
fi
exec "$REVIEW_REAL_GH" "$@"
''')
            shim.chmod(0o700)
            env['PATH'] = str(root) + os.pathsep + env['PATH']
        script = next(s['run'] for s in steps if s['name'] == 'Publish version check result')
        try:
            result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
                                    env=env, text=True, capture_output=True, timeout=120)
        finally:
            if server:
                server.shutdown()
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        latest = next(s for s in statuses(pr['head']['sha']) if s['context'] == 'group-version-guard')
        assert result.returncode == 0 and latest['state'] == 'success', (result.returncode, latest)
        if os.environ['INJECT_FAILURE'] == 'true':
            assert requests, 'fault injection was not reached'
            summary('REPRODUCED: the real gh CLI received HTTP 503 during the final refresh, '
                    'but the original shell step exited 0 and posted a real `success` status. '
                    'The 503 was generated by a local test endpoint, not a GitHub outage.')
        else:
            summary('CONTROL PASSED: the original guard read the real PR and posted `success` '
                    'with no fault injection.')
        summary(f'PR: [{pr["html_url"]}]({pr["html_url"]}); status creator: '
                f'`{latest["creator"]["login"]}`; status URL: {latest["target_url"]}.')


def template_sync():
    protected = 'review/template-protected'
    control = 'review/template-unprotected'
    branches = [control, protected]
    before = {b: api(f'repos/{REPO}/git/ref/heads/{b}')['object']['sha'] for b in branches}
    settings = api(f'repos/{REPO}/branches/{quote(protected, safe="")}')
    assert settings['protected']
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = root / 'key'
        key.write_text(os.environ['REVIEW_SYNC_SSH_KEY'] + '\n')
        key.chmod(0o600)
        known_hosts = root / 'known_hosts'
        known_hosts.write_text(''.join('github.com ' + k + '\n' for k in api('meta')['ssh_keys']))
        executable = root / 'action-sync'
        subprocess.run(['go', 'build', '-o', str(executable), 'sources/action-sync-main.go'],
                       check=True, timeout=120)
        env = {**os.environ, 'GIT_SSH_COMMAND':
               f'ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}'}
        subprocess.run(['git', 'config', '--global', 'user.name', 'release-review'], check=True)
        subprocess.run(['git', 'config', '--global', 'user.email', 'release-review@users.noreply.github.com'], check=True)
        result = subprocess.run([str(executable), '-files=sync-config.json', '-message=review: sync template'],
                                text=True, capture_output=True, env=env, timeout=240)
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
    after = {b: api(f'repos/{REPO}/git/ref/heads/{b}')['object']['sha'] for b in branches}
    assert result.returncode == 0, result.returncode
    assert after[control] != before[control], 'unprotected control push failed; cannot attribute rejection to protection'
    assert after[protected] == before[protected], 'protected branch was unexpectedly updated'
    assert '::error::' in result.stdout and ('GH006' in result.stdout or 'GH013' in result.stdout), result.stdout
    summary('REPRODUCED: the same SSH deploy key and original action-sync binary updated the '
            'unprotected branch, while GitHub rejected the protected branch. '
            'action-sync still exited 0. No bypass was configured.')
    summary('```json\n' + json.dumps({'before': before, 'after': after, 'exit_code': result.returncode}, indent=2) + '\n```')


verify_sources()
{'race': race, 'guard': guard, 'template': template_sync}[sys.argv[1]]()

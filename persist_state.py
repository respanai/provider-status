"""Keep non-secret reconciliation state on a public repository's state branch."""

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def request(method, path, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        'https://api.github.com' + path,
        data=data,
        method=method,
        headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
            'User-Agent': 'official-provider-status-sync',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'GitHub state operation failed with HTTP {exc.code}') from None


def persist(state_path, repository, token, call=request):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('GITHUB_REPOSITORY must identify one owner/repository.')
    metadata = call('GET', f'/repos/{repository}', token)
    if metadata.get('private') is not False:
        raise ValueError('This free deployment requires a public repository.')
    state = json.loads(Path(state_path).read_text())
    serialized = (json.dumps(state, sort_keys=True, indent=2) + '\n').encode()
    current = call('GET', f'/repos/{repository}/contents/state.json?ref=state', token)
    prior = base64.b64decode(current['content'])
    if prior == serialized:
        return False
    # State intentionally contains only provider observations and public resource IDs.
    # Never store tokens, request headers, response headers, or exception bodies.
    if token and token.encode() in serialized:
        raise ValueError('Refusing to publish a credential in synchronization state.')
    call('PUT', f'/repos/{repository}/contents/state.json', token, {
        'message': 'Update provider status reconciliation state',
        'branch': 'state',
        'sha': current['sha'],
        'content': base64.b64encode(serialized).decode(),
    })
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise SystemExit('GITHUB_TOKEN is required to preserve reconciliation state.')
    changed = persist(args.state, os.environ.get('GITHUB_REPOSITORY', ''), token)
    print('Reconciliation state saved.' if changed else 'Reconciliation state unchanged.')


if __name__ == '__main__':
    main()

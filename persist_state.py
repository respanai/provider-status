"""Keep public provider snapshots and reconciliation state on the state branch."""

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


class GitHubStateError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'GitHub state operation failed with HTTP {status}')


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
        raise GitHubStateError(exc.code) from None


def persist(state_path, repository, token, call=request, remote_path='state.json'):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('GITHUB_REPOSITORY must identify one owner/repository.')
    if remote_path not in {'state.json', 'public-status.json'}:
        raise ValueError('Only reviewed JSON files may be published to the state branch.')
    metadata = call('GET', f'/repos/{repository}', token)
    if metadata.get('private') is not False:
        raise ValueError('This free deployment requires a public repository.')
    state = json.loads(Path(state_path).read_text())
    serialized = (json.dumps(state, sort_keys=True, indent=2) + '\n').encode()
    try:
        current = call('GET', f'/repos/{repository}/contents/{remote_path}?ref=state', token)
    except GitHubStateError as exc:
        if exc.status != 404 or remote_path != 'public-status.json':
            raise
        current = None
    if current and base64.b64decode(current['content']) == serialized:
        return False
    # State intentionally contains only provider observations and public resource IDs.
    # Never store tokens, request headers, response headers, or exception bodies.
    if token and token.encode() in serialized:
        raise ValueError('Refusing to publish a credential in synchronization state.')
    payload = {
        'message': ('Update public provider status snapshot' if remote_path == 'public-status.json'
                    else 'Update provider status reconciliation state'),
        'branch': 'state',
        'content': base64.b64encode(serialized).decode(),
    }
    if current:
        payload['sha'] = current['sha']
    call('PUT', f'/repos/{repository}/contents/{remote_path}', token, payload)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--remote-path', choices=['state.json', 'public-status.json'], default='state.json')
    args = parser.parse_args()
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise SystemExit('GITHUB_TOKEN is required to preserve reconciliation state.')
    changed = persist(args.state, os.environ.get('GITHUB_REPOSITORY', ''), token,
                      remote_path=args.remote_path)
    print(f'{args.remote_path} saved.' if changed else f'{args.remote_path} unchanged.')


if __name__ == '__main__':
    main()

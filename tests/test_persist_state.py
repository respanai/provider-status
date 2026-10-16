import base64
import json
import tempfile
import unittest
from pathlib import Path

from persist_state import persist


class StatePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'

    def test_unchanged_state_does_not_commit(self):
        self.path.write_text('{}\n')
        calls = []

        def api(method, path, token, payload=None):
            calls.append((method, path))
            if '/contents/' in path:
                return {'sha': 'current', 'content': base64.b64encode(b'{}\n').decode()}
            return {'private': False}

        self.assertFalse(persist(self.path, 'example/status', 'not-a-real-key', api))
        self.assertEqual([method for method, _ in calls], ['GET', 'GET'])

    def test_private_repository_is_rejected_before_content_write(self):
        self.path.write_text('{}\n')
        calls = []

        def api(method, path, token, payload=None):
            calls.append(method)
            return {'private': True}

        with self.assertRaisesRegex(ValueError, 'public repository'):
            persist(self.path, 'example/status', 'not-a-real-key', api)
        self.assertEqual(calls, ['GET'])

    def test_token_cannot_be_published(self):
        self.path.write_text(json.dumps({'accidental': 'not-a-real-key'}))
        calls = []

        def api(method, path, token, payload=None):
            calls.append(method)
            if '/contents/' in path:
                return {'sha': 'current', 'content': base64.b64encode(b'{}\n').decode()}
            return {'private': False}

        with self.assertRaisesRegex(ValueError, 'credential'):
            persist(self.path, 'example/status', 'not-a-real-key', api)
        self.assertNotIn('PUT', calls)

    def test_changed_public_state_updates_only_state_branch(self):
        self.path.write_text(json.dumps({'providers': {'openai': {'resource_id': '123'}}}))
        writes = []

        def api(method, path, token, payload=None):
            if method == 'PUT':
                writes.append(payload)
                return {}
            if '/contents/' in path:
                return {'sha': 'current', 'content': base64.b64encode(b'{}\n').decode()}
            return {'private': False}

        self.assertTrue(persist(self.path, 'example/status', 'not-a-real-key', api))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['branch'], 'state')
        self.assertEqual(writes[0]['sha'], 'current')


if __name__ == '__main__':
    unittest.main()

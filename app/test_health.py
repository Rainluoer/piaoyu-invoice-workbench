"""就绪检查不受完整台账扫描影响，旧服务与端口冲突仍能识别。"""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import launcher
from server import Handler, LocalServer


class HealthTests(unittest.TestCase):
    def test_existing_service_is_reused_while_ledger_is_locked(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            store = SimpleNamespace(data_dir=base / 'data', state=Mock(side_effect=AssertionError('must not read ledger')), lock=threading.Lock())
            http = LocalServer(('127.0.0.1', 0), Handler)
            http.store = store
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{http.server_port}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with store.lock, patch('launcher.subprocess.Popen') as process:
                    self.assertEqual(launcher.launch(base, http.server_port, False), url)
                    process.assert_not_called()
                    store.state.assert_not_called()
                with opener.open(url + '/api/health') as response:
                    state = json.load(response)
                self.assertEqual(state, {'app':'piaoyu-invoice-workbench','data_dir':str(base / 'data')})
                request = urllib.request.Request(url + '/api/health', headers={'Host':'untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request)
                self.assertEqual(error.exception.code, 403)
            finally:
                http.shutdown(); http.server_close(); thread.join(timeout=3)

    def test_legacy_service_falls_back_only_when_health_is_missing(self):
        data_dir = Path('legacy-data').resolve()
        response = io.BytesIO(json.dumps({'data_dir':str(data_dir)}).encode())
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('http://local/api/health',404,'missing',{},None), response]
        self.assertTrue(launcher.running_service(opener, 'http://local', data_dir))
        self.assertEqual([call.args[0] for call in opener.open.call_args_list], ['http://local/api/health','http://local/api/state'])

    def test_unrelated_health_response_is_not_accepted_as_workbench(self):
        data_dir = Path('other-data').resolve()
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({'app':'different-app','data_dir':str(data_dir)}).encode())
        with self.assertRaises(RuntimeError):
            launcher.running_service(opener, 'http://local', data_dir)

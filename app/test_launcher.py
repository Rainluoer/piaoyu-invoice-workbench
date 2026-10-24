"""验证无窗口服务与启动器分离、重复启动和失败诊断，不读取真实票据。"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import time
import unittest
from unittest.mock import patch
import urllib.request
import venv

import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        pid = None
        try:
            with self.opener.open(self.url + '/stop', timeout=0.5) as response:
                # 在关闭监听端口之外，还要等待解释器释放日志及虚拟环境可执行文件。
                pid = json.load(response)['pid']
            for _ in range(30):
                try:
                    with self.opener.open(self.url + '/api/state', timeout=0.1):
                        pass
                except OSError:
                    break
                time.sleep(0.05)
        except OSError:
            pass
        if os.name == 'nt' and pid is not None:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0, 'synthetic service did not exit')
                finally:
                    kernel.CloseHandle(handle)
        # Windows 上监听端口关闭后，解释器还可能短暂持有日志文件句柄。
        for _ in range(40):
            try:
                self.temp.cleanup()
                return
            except (PermissionError, NotADirectoryError):
                time.sleep(0.05)
        self.temp.cleanup()

    def write_service(self, base):
        (base / 'server.py').write_text(textwrap.dedent('''
            import argparse, ctypes, json, os, threading
            from pathlib import Path
            from http.server import BaseHTTPRequestHandler, HTTPServer
            parser = argparse.ArgumentParser()
            parser.add_argument('--port', type=int)
            parser.add_argument('--no-browser', action='store_true')
            parser.add_argument('--ignore-dir', action='append', default=[])
            args = parser.parse_args()
            base = Path(__file__).resolve().parent
            with (base/'starts.txt').open('a') as f: f.write('start\\n')
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps({'app':'piaoyu-invoice-workbench', 'data_dir':str(base/'data'), 'pid':os.getpid(), 'ignored':args.ignore_dir,
                        'console':int(ctypes.windll.kernel32.GetConsoleWindow()) if os.name=='nt' else 0}).encode())
                    if self.path == '/stop': threading.Thread(target=http.shutdown,daemon=True).start()
            http = HTTPServer(('127.0.0.1',args.port),Handler)
            print('synthetic service ready',flush=True)
            try: http.serve_forever()
            finally: http.server_close()
        '''), encoding='utf-8')

    def test_parent_exits_hidden_child_survives_and_repeated_launch_reuses_it(self):
        self.write_service(self.base)
        command = [sys.executable, '-c', 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import launcher; launcher.launch(Path(sys.argv[2]),int(sys.argv[3]),False)',
                   str(Path(launcher.__file__).parent), str(self.base), str(self.port)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        with self.opener.open(self.url + '/api/state') as response:
            before = json.load(response)
        self.assertEqual(before['console'], 0)
        self.assertEqual((self.base / 'data/server.log').read_text().strip(), 'synthetic service ready')
        second = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        with self.opener.open(self.url + '/api/state') as response:
            self.assertEqual(json.load(response)['pid'], before['pid'])
        self.assertEqual((self.base / 'starts.txt').read_text().splitlines(), ['start'])

    @unittest.skipUnless(os.name == 'nt', 'Windows CMD entry point')
    def test_windows_cmd_exits_and_keeps_hidden_service_in_unicode_folder(self):
        repo = Path(launcher.__file__).resolve().parent.parent
        root = self.base / '工作台 测试'
        app = root / 'app'
        app.mkdir(parents=True)
        (root / 'scripts').mkdir()
        self.write_service(app)
        shutil.copyfile(launcher.__file__, app / 'launcher.py')
        shutil.copyfile(repo / 'scripts/start.ps1', root / 'scripts/start.ps1')
        shutil.copyfile(repo / '启动工作台.cmd', root / '启动工作台.cmd')
        venv.EnvBuilder(with_pip=False).create(root / '.venv')
        ignored = root / '忽略 目录'
        command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{root / "启动工作台.cmd"}" --port {self.port} --no-browser --ignore-dir "{ignored}""'
        first = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        with self.opener.open(self.url + '/api/health') as response:
            before = json.load(response)
        self.assertEqual(before['console'], 0)
        self.assertEqual(before['ignored'], [str(ignored)])
        second = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        with self.opener.open(self.url + '/api/health') as response:
            self.assertEqual(json.load(response)['pid'], before['pid'])
        self.assertEqual((app / 'starts.txt').read_text().splitlines(), ['start'])

    def test_conflicting_service_does_not_start_another_process(self):
        with patch('launcher.running_service', side_effect=RuntimeError('port occupied')), patch('launcher.subprocess.Popen') as process:
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                launcher.launch(self.base, self.port, False)
            process.assert_not_called()

    def test_failed_child_returns_error_and_preserves_log(self):
        (self.base / 'server.py').write_text('import sys\nprint("synthetic startup error",file=sys.stderr)\nraise SystemExit(9)\n',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'server-error.log'):
            launcher.launch(self.base, self.port, False, timeout=5)
        self.assertIn('synthetic startup error', (self.base / 'data/server-error.log').read_text())

    def test_existing_service_only_opens_browser(self):
        with patch('launcher.running_service', return_value=True), patch('launcher.subprocess.Popen') as process, patch('launcher.webbrowser.open') as browser:
            self.assertEqual(launcher.launch(self.base, self.port), self.url)
            process.assert_not_called()
            browser.assert_called_once_with(self.url)

    def test_ignore_directories_are_forwarded_as_separate_arguments(self):
        directories = [str(self.base / "测试 空格'`$目录"), str(self.base / '其他目录')]
        with patch('launcher.running_service', side_effect=[False, True]), patch('launcher.subprocess.Popen') as process:
            launcher.launch(self.base, self.port, False, ignore_dirs=directories)
        command = process.call_args.args[0]
        self.assertEqual(command[-4:], ['--ignore-dir', directories[0], '--ignore-dir', directories[1]])
        self.assertEqual(process.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(process.call_args.kwargs['close_fds'])


if __name__ == '__main__':
    unittest.main()

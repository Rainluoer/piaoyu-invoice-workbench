"""短时启动器：服务独立后台运行，启动窗口可立即退出。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


def running_service(opener, url, data_dir):
    legacy = False
    try:
        try:
            response = opener.open(url + '/api/health', timeout=0.5)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # 兼容修复前仍在运行的服务，更新启动器时不强制停止它。
            legacy = True
            response = opener.open(url + '/api/state', timeout=0.5)
        with response:
            state = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('启动端口已被其他程序占用，请检查本地服务。') from error
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('启动端口返回了其他程序的页面，请检查端口占用。') from error
    if not isinstance(state, dict) or state.get('data_dir') != str(data_dir) or (not legacy and state.get('app') != 'piaoyu-invoice-workbench'):
        raise RuntimeError('该端口已被其他工作台或程序使用，请关闭冲突程序后重试。')
    return True


def launch(base=None, port=18626, open_browser=True, timeout=30, ignore_dirs=None):
    base = Path(base or Path(__file__).resolve().parent).resolve()
    data_dir = base / 'data'
    url = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if not running_service(opener, url, data_dir):
        data_dir.mkdir(parents=True, exist_ok=True)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        command = [sys.executable, '-u', str(base / 'server.py'), '--port', str(port), '--no-browser']
        for directory in ignore_dirs or []:
            command.extend(['--ignore-dir', str(directory)])
        # 子进程直接继承日志文件句柄，不依赖启动器存活或父窗口的控制台。
        with (data_dir / 'server.log').open('ab', buffering=0) as output, (data_dir / 'server-error.log').open('ab', buffering=0) as errors:
            process = subprocess.Popen(command,
                cwd=base, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                close_fds=True, **options)
        deadline = time.monotonic() + timeout
        while not running_service(opener, url, data_dir):
            if process.poll() is not None:
                raise RuntimeError(f'工作台未能启动，请查看日志：{data_dir / "server-error.log"}')
            if time.monotonic() >= deadline:
                raise RuntimeError(f'工作台尚未就绪，请稍后重试；启动日志：{data_dir / "server-error.log"}')
            time.sleep(0.15)
    if open_browser:
        webbrowser.open(url)
    return url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18626)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--ignore-dir', action='append', default=[], help='不参与扫描的完整目录路径，可重复指定')
    args = parser.parse_args()
    try:
        launch(port=args.port, open_browser=not args.no_browser, ignore_dirs=args.ignore_dir)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

from __future__ import annotations

import json
from pathlib import Path

from cater.app.cli import main


def test_cli_version_init_health_smoke(tmp_path: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    assert main(["--runtime", str(runtime), "--json", "init"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["database"]).is_file()
    assert Path(payload["logs_dir"]).is_dir()
    assert payload["schema_version"] >= 4

    assert main(["--runtime", str(runtime), "--json", "health"]) == 0
    health = json.loads(capsys.readouterr().out)
    assert health["quick_check"] == "ok"
    assert health["dry_run"] is True
    assert health["sending_enabled"] is False

def _module_env():
    import os
    env = {key: value for key, value in os.environ.items() if not key.startswith('CATER_')}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2] / 'src')
    return env


def test_cli_module_executes_commands_and_reports_errors(tmp_path):
    import subprocess
    import sys
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'cater.app.cli', *args],
                              cwd=tmp_path, env=_module_env(), capture_output=True,
                              text=True, timeout=20)
    result = run('version')
    assert result.returncode == 0 and result.stdout.strip() == '0.1.0'
    result = run('--runtime', str(tmp_path / 'runtime'), '--json', 'init')
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['database']).is_file()
    result = run('--config', str(tmp_path / 'missing.toml'), 'health')
    assert result.returncode == 2
    assert 'configuration error' in result.stderr


def test_cli_module_serve_stays_alive_and_serves_health(tmp_path):
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = tmp_path / 'server.log'
    with log.open('w') as output:
        process = subprocess.Popen(
            [sys.executable, '-m', 'cater.app.cli', '--runtime', str(tmp_path / 'runtime'),
             'serve', '--no-poll', '--port', str(port)],
            cwd=tmp_path, env=_module_env(), stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, log.read_text()
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
                        health = json.load(response)
                    assert health['quick_check'] == 'ok'
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError('Server did not start: ' + log.read_text())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_windows_server_selects_custom_loop_without_global_policy(monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import cater.app.cli as cli
    import cater.app.web as web
    import uvicorn
    config = SimpleNamespace(web=SimpleNamespace(host='127.0.0.1', port=8766))
    monkeypatch.setattr(cli, 'load_config', lambda *_: config)
    monkeypatch.setattr(cli, '_service', lambda *_: nullcontext(object()))
    monkeypatch.setattr(web, 'create_app', lambda *_, **__: object())
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(cli.sys, 'platform', 'win32')
    args = SimpleNamespace(config=None, runtime=None, host=None, port=None, no_poll=True)
    assert cli.cmd_serve(args) == 0
    assert captured['loop'] == 'cater.app.event_loop:new_server_loop'
    monkeypatch.setattr(cli.sys, 'platform', 'darwin')
    cli.cmd_serve(args)
    assert captured['loop'] == 'auto'


def test_uvicorn_resolves_and_runs_selector_loop_factory():
    import asyncio
    import uvicorn
    config = uvicorn.Config('cater.app.web:create_app', loop='cater.app.event_loop:new_server_loop')
    factory = config.get_loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        async def probe():
            return 42
        assert loop.run_until_complete(probe()) == 42
        assert loop.get_exception_handler() is None
    finally:
        loop.close()

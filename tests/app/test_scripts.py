from __future__ import annotations

import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
HELPER = SCRIPTS / "native.ps1"
DEPLOYMENT_SCRIPTS = ("install.ps1", "run-web.ps1", "run-poller.ps1", "backup.ps1")
PWSH = shutil.which("pwsh")


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


def test_every_deployment_native_call_goes_through_helper() -> None:
    helper = _read("native.ps1")
    assert "$LASTEXITCODE" in helper
    assert "throw $Message" in helper
    assert "exit $ExitCode" in helper

    for name in DEPLOYMENT_SCRIPTS:
        text = _read(name)
        assert '. (Join-Path $PSScriptRoot "native.ps1")' in text, name
        assert "Invoke-CaterNative" in text, name
        # The helper is the only place allowed to use the native call operator.
        assert not re.search(r"(?m)^\s*&\s", text), name

    install = _read("install.ps1")
    assert not re.search(r"(?m)^\s*py\s", install)
    assert install.count("Invoke-CaterNative") >= 4
    init_at = install.index('-Description "Initialising the CATER runtime"')
    installed_at = install.index('Write-Host "Installed. Next steps:"')
    assert init_at < installed_at, "install.ps1 must not claim success before init"

    for name in ("run-web.ps1", "run-poller.ps1"):
        assert "-ExitOnFailure" in _read(name), name

    # register-tasks.ps1 is cmdlet-only; guard against a future raw native call.
    register = _read("register-tasks.ps1")
    assert not re.search(r"(?m)^\s*&\s", register)
    assert "& $Python" not in register
    assert "py -3" not in register


def test_operations_records_test_environment_and_windows_boundary() -> None:
    operations = (REPO_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for text, name in ((operations, "operations"), (readme, "README")):
        assert "pytest 9.1.1" in text, name
        assert "lxml 6.1.1" in text, name
        assert "soupsieve 2.9.1" in text, name
    assert "9.2.0" not in operations
    assert "9.2.0" not in readme
    assert "Windows" in operations and "未在 Windows 目标机验收" in operations


def test_dependency_pins_match_installed_test_versions() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    core_deps = (REPO_ROOT / "docs" / "core-dependencies.txt").read_text(
        encoding="utf-8"
    )
    for name in ("lxml", "soupsieve", "pytest"):
        installed = metadata.version(name)
        assert f'"{name}=={installed}"' in pyproject, (name, installed)
        assert f"{name}=={installed}" in core_deps, (name, installed)


def _probe(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    helper = str(HELPER).replace("'", "''")
    python = sys.executable.replace("'", "''")
    script = tmp_path / "probe.ps1"
    script.write_text(
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        f". '{helper}'\n"
        + body.replace("__PYTHON__", python)
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")
def test_helper_throws_on_native_failure(tmp_path: Path) -> None:
    result = _probe(
        tmp_path,
        """
try {
    Invoke-CaterNative -Description "probe failure" -FilePath '__PYTHON__' `
        -Arguments @('-c', 'raise SystemExit(7)')
    exit 90
} catch {
    Write-Output $_.Exception.Message
    exit 42
}
""",
    )
    assert result.returncode == 42
    assert "probe failure failed with exit code 7" in result.stdout + result.stderr


@pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")
def test_helper_can_propagate_native_exit_code(tmp_path: Path) -> None:
    result = _probe(
        tmp_path,
        """
Invoke-CaterNative -Description "probe exit" -FilePath '__PYTHON__' `
    -Arguments @('-c', 'raise SystemExit(9)') -ExitOnFailure
exit 90
""",
    )
    assert result.returncode == 9
    assert "probe exit failed with exit code 9" in result.stdout + result.stderr


@pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")
def test_helper_allows_native_success(tmp_path: Path) -> None:
    result = _probe(
        tmp_path,
        """
Invoke-CaterNative -Description "probe success" -FilePath '__PYTHON__' `
    -Arguments @('-c', 'raise SystemExit(0)')
exit 0
""",
    )
    assert result.returncode == 0

def test_environment_launchers_are_utf8_and_bind_separate_configs(tmp_path):
    from cater.app.config import load_config
    for environment, port in [('test', 8766), ('production', 8765)]:
        launcher = (REPO_ROOT / f'start-{environment}.bat').read_text(encoding='utf-8')
        assert 'chcp 65001' in launcher
        assert f'-Config "config\\{environment}.local.toml"' in launcher
        assert f'-Runtime "runtime\\{environment}"' in launcher
        assert f'-Port {port}' in launcher
        config = load_config(REPO_ROOT / 'config' / f'{environment}.example.toml',
                             runtime_dir=tmp_path / environment)
        assert config.allowlist.mode == environment
        assert config.sending.dry_run and not config.sending.enabled
        assert config.web.port == port
    script = _read('run-web.ps1')
    assert '@("--config", $Config)' in script
    assert '@("--runtime", $Runtime)' in script


def test_interactive_launchers_keep_console_and_initialize_test_only():
    for name in ('start-test.bat', 'start-production.bat', 'initialize-test.bat'):
        content = (REPO_ROOT / name).read_bytes()
        assert b'\r\n' in content and b'\\r\\n' not in content
        text = content.decode('utf-8')
        assert 'chcp 65001' in text
        assert '"%ComSpec%" /D /K call "%~f0" --console' in text
        assert 'if /I "%~1"=="--console" goto run' in text
        assert 'Exit code: %CATER_EXIT_CODE%' in text
    for name in ('start-test.bat', 'start-production.bat'):
        assert '-OpenBrowser' in (REPO_ROOT / name).read_text()
    initializer = _read('initialize-test.ps1')
    assert 'runtime/test' in initializer and 'runtime/production' not in initializer
    assert '"import-machines", $ReferencePath' in initializer
    assert 'OpenFileDialog' in initializer
    assert '"settings", "--json"' in initializer
    assert '"--enable"' not in initializer
    launcher = _read('run-web.ps1')
    assert 'api/health' in launcher and 'Start-Process $Url' in launcher

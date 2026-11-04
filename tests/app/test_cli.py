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
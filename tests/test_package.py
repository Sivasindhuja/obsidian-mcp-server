from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


def test_main_module_invokes_main(monkeypatch):
    import mcp_obsidian.main as main_module

    called = []
    monkeypatch.setattr(main_module, "main", lambda: called.append(True))

    runpy.run_module("mcp_obsidian", run_name="__main__")

    assert called == [True]


def test_python_m_reports_missing_api_key(tmp_path):
    env = os.environ.copy()
    env.pop("OBSIDIAN_API_KEY", None)
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), env.get("PYTHONPATH", "")]
    )

    result = subprocess.run(
        [sys.executable, "-m", "mcp_obsidian"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "OBSIDIAN_API_KEY" in result.stderr

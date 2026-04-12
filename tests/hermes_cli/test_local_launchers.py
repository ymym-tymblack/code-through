"""Tests for repo-local CLI launcher scripts."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_local_launchers_delegate_to_main(monkeypatch):
    called: list[str] = []

    fake_main_module = ModuleType("hermes_cli.main")

    def _fake_main():
        called.append("main")

    fake_main_module.main = _fake_main
    monkeypatch.setitem(sys.modules, "hermes_cli.main", fake_main_module)

    for launcher_name in ("hermes", "code-through"):
        called.clear()
        runpy.run_path(str(REPO_ROOT / launcher_name), run_name="__main__")
        assert called == ["main"]

"""A source preview must never discover credentials in its parent workspace."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from careeros.config import load_environment


def test_environment_loading_is_explicit_local_and_preserves_existing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "preview"
    child.mkdir()
    (tmp_path / ".env").write_text("CAREEROS_TEST_PARENT_ONLY=parent-value\n")
    monkeypatch.delenv("CAREEROS_TEST_PARENT_ONLY", raising=False)
    monkeypatch.delenv("CAREEROS_TEST_LOCAL", raising=False)
    monkeypatch.chdir(child)
    load_environment()
    assert "CAREEROS_TEST_PARENT_ONLY" not in os.environ
    (child / ".env").write_text("CAREEROS_TEST_LOCAL=local-value\n")
    load_environment()
    assert os.environ["CAREEROS_TEST_LOCAL"] == "local-value"
    monkeypatch.setenv("CAREEROS_TEST_LOCAL", "explicit-value")
    load_environment()
    assert os.environ["CAREEROS_TEST_LOCAL"] == "explicit-value"


def test_importing_connectors_never_loads_parent_or_local_dotenv(tmp_path: Path) -> None:
    child = tmp_path / "preview"
    child.mkdir()
    for folder in (tmp_path, child):
        (folder / ".env").write_text("CAREEROS_TEST_IMPORT_SENTINEL=must-not-load\n")
    script = (
        "import importlib, os, pkgutil, careeros.connectors; "
        "[importlib.import_module(m.name) for m in "
        "pkgutil.iter_modules(careeros.connectors.__path__, 'careeros.connectors.')]; "
        "assert 'CAREEROS_TEST_IMPORT_SENTINEL' not in os.environ"
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    subprocess.run([sys.executable, "-c", script], cwd=child, env=environment, check=True)

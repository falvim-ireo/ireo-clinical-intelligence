"""Teste local opt-in da instalação oficial do UnRAR."""

import os
from pathlib import Path
import subprocess

import pytest


UNRAR = Path(r"C:\Program Files\WinRAR\UnRAR.exe")


pytestmark = pytest.mark.skipif(
    os.getenv("IREO_RUN_LOCAL_UNRAR_TEST") != "1" or not UNRAR.is_file(),
    reason="teste local do UnRAR requer opt-in e instalação oficial",
)


def test_installed_unrar_lists_fixture_without_window(tmp_path: Path) -> None:
    archive = Path(os.environ["IREO_LOCAL_RAR_FIXTURE"])
    completed = subprocess.run(
        [str(UNRAR), "l", str(archive)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0
    assert "Attributes" in completed.stdout

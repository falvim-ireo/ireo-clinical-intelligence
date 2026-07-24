"""Testes unitários da extração segura e multiplataforma de arquivos."""

import logging
from pathlib import Path
import subprocess

import pytest

from radiology.archive_extractor import ArchiveExtractionError, ArchiveExtractor


def test_unrar_destination_ends_with_backslash_on_windows(tmp_path: Path) -> None:
    destination = tmp_path / "quarantine" / "exam"

    argument = ArchiveExtractor._destination_argument(destination, os_name="nt")

    assert argument == f"{destination}\\"


@pytest.mark.parametrize("os_name", ["posix", "darwin"])
def test_unrar_destination_ends_with_slash_on_macos_and_linux(
    tmp_path: Path,
    os_name: str,
) -> None:
    destination = tmp_path / "quarantine" / "exam"

    argument = ArchiveExtractor._destination_argument(destination, os_name=os_name)

    assert argument == f"{destination}/"


def test_unrar_failure_logs_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    archive = tmp_path / "sensitive-patient-name.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "unrar"
    tool.write_bytes(b"tool")
    sensitive_stderr = f"Cannot open {archive.resolve()}\n" + ("detail " * 100)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 7, "", sensitive_stderr
        ),
    )
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)

    with caplog.at_level(logging.WARNING), pytest.raises(
        ArchiveExtractionError, match="não conseguiu"
    ):
        extractor.extract(archive)

    assert "returncode=7" in caplog.text
    assert "stderr=Cannot open <redacted>" in caplog.text
    assert str(archive.resolve()) not in caplog.text
    assert len(caplog.records[0].getMessage()) < 320


def test_unrar_success_uses_platform_destination_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "exam.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "unrar"
    tool.write_bytes(b"tool")
    calls: list[tuple[list[str], dict]] = []

    def successful_run(command: list[str], **kwargs):
        calls.append((command, kwargs))
        stdout = "scan/image.dcm\n" if command[1] == "lb" else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", successful_run)
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)

    destination = extractor.extract(archive)

    assert destination.is_dir()
    assert len(calls) == 2
    assert calls[1][0] == [
        str(tool),
        "x",
        "-o-",
        str(archive.resolve()),
        f"{destination}/",
    ]
    assert calls[1][1]["shell"] is False

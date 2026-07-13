"""Testes totalmente offline do MVP supervisionado de radiologia."""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
import requests

from integrations.onedrive_connector import (
    PatientFolderError,
    PatientFolderLocator,
)
from models.email_message import EmailMessage
from models.patient import Patient
from observability.audit_logger import AuditLogger, mask_patient_id
from radiology.archive_extractor import (
    ArchiveExtractionError,
    ArchiveExtractor,
)
from radiology.supervised_import import (
    ConfirmedPatient,
    SupervisedImportCancelled,
    SupervisedImportError,
    SupervisedRadiologyImporter,
)
from repositories.patient_repository import InMemoryPatientRepository
from repositories.patient_repository import EmptyPatientRepository


PATIENT_NAME = "CLÁUDIA EXEMPLO FICTÍCIA"


@pytest.fixture(autouse=True)
def forbid_external_actions(monkeypatch) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Acesso de rede proibido no teste supervisionado")

    monkeypatch.setattr(requests, "get", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)


def create_zip(path: Path, files: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in (files or {"scan/image.dcm": b"dicom-ficticio"}).items():
            archive.writestr(name, content)
    return path


def input_sequence(*answers: str):
    values = iter(answers)
    return lambda prompt: next(values)


def build_importer(
    tmp_path: Path,
    *,
    answers: tuple[str, ...] = ("1", "1", "CONFIRMAR"),
    patients: list[Patient] | None = None,
    patient_folders: tuple[str, ...] = (PATIENT_NAME,),
    gmail_connector=None,
    downloader=None,
) -> tuple[SupervisedRadiologyImporter, Path, Path, list[str]]:
    patients_root = tmp_path / "patients"
    patients_root.mkdir(exist_ok=True)
    for folder_name in patient_folders:
        (patients_root / folder_name).mkdir(exist_ok=True)
    quarantine = tmp_path / "quarantine"
    tool = tmp_path / "WinRAR.exe"
    tool.write_bytes(b"ferramenta-ficticia")
    output: list[str] = []
    audit = AuditLogger(correlation_id="correlation-supervised-0001")
    importer = SupervisedRadiologyImporter(
        patients_root=patients_root,
        quarantine_root=quarantine,
        archive_tool_path=tool,
        patient_repository=InMemoryPatientRepository(
            patients
            if patients is not None
            else [Patient(id=990000010, nome=PATIENT_NAME)]
        ),
        gmail_connector=gmail_connector,
        audit_logger=audit,
        input_func=input_sequence(*answers),
        output=output.append,
        downloader=downloader,
        today_provider=lambda: date(2099, 12, 31),
        now_provider=lambda: datetime(
            2099,
            12,
            31,
            23,
            59,
            tzinfo=timezone.utc,
        ),
    )
    return importer, patients_root, quarantine, output


def test_local_zip_runs_complete_supervised_copy_and_manifest(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {
            "scan/image-1.dcm": b"primeiro-conteudo-ficticio",
            "report.txt": b"laudo-ficticio",
        },
    )
    importer, _, _, output = build_importer(tmp_path)

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.destination.name == "2099-12-31 - Radiologia"
    assert result.file_count == 2
    assert result.total_size_bytes == sum(
        len(value)
        for value in (
            b"primeiro-conteudo-ficticio",
            b"laudo-ficticio",
        )
    )
    assert archive.exists()
    assert (result.destination / "scan" / "image-1.dcm").is_file()
    assert manifest["correlation_id"] == "correlation-supervised-0001"
    assert manifest["timestamp_utc"] == "2099-12-31T23:59:00Z"
    assert manifest["original_archive"] == archive.name
    assert manifest["masked_patient_id"] == mask_patient_id(990000010)
    assert manifest["source"] == "local"
    assert manifest["status"] == "COMPLETED"
    assert manifest["destination"] == str(result.destination)
    assert any(line.startswith("Arquivo de origem:") for line in output)
    assert "Quantidade de arquivos: 2" in output
    assert "Possíveis duplicados: 0" in output


def test_checksums_match_every_copied_file(tmp_path: Path) -> None:
    content = b"checksum-ficticio"
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"nested/scan.bin": content},
    )
    importer, _, _, _ = build_importer(tmp_path)

    result = importer.run(archive_path=archive)

    expected = hashlib.sha256(content).hexdigest()
    assert result.checksums == {"nested/scan.bin": expected}
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["checksums"] == {"nested/scan.bin": expected}


def test_local_rar_uses_winrar_without_shell_and_keeps_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / f"{PATIENT_NAME}_20991231.rar"
    archive.write_bytes(b"rar-ficticio")
    importer, _, _, _ = build_importer(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "lb":
            return subprocess.CompletedProcess(command, 0, "scan/image.dcm\n", "")
        destination = Path(command[-1].rstrip("\\"))
        (destination / "scan").mkdir(parents=True, exist_ok=True)
        (destination / "scan" / "image.dcm").write_bytes(b"rar-extraido")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = importer.run(archive_path=archive)

    assert archive.exists()
    assert result.file_count == 1
    assert len(calls) == 2
    assert calls[0][0][0].endswith("WinRAR.exe")
    assert calls[0][0][1:] == ["lb", "-p-", str(archive.resolve())]
    assert calls[1][0][1:4] == ["x", "-o-", "-p-"]
    assert all(call_kwargs["shell"] is False for _, call_kwargs in calls)
    assert all(call_kwargs["timeout"] == 120 for _, call_kwargs in calls)


def test_winrar_not_found_is_safe(tmp_path: Path) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    extractor = ArchiveExtractor(
        tmp_path / "quarantine",
        tmp_path / "missing-winrar.exe",
    )

    with pytest.raises(ArchiveExtractionError, match="não foi encontrado"):
        extractor.extract(archive)

    assert archive.exists()


def test_winrar_failure_is_checked(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "WinRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "secret"),
    )

    with pytest.raises(ArchiveExtractionError, match="não conseguiu"):
        extractor.extract(archive)


def test_winrar_timeout_is_safe(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "WinRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool, timeout_seconds=1)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output="sensitive")

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ArchiveExtractionError, match="tempo limite"):
        extractor.extract(archive)


def test_missing_local_archive_is_rejected(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(tmp_path)

    with pytest.raises(SupervisedImportError, match="não existe"):
        importer.run(archive_path=tmp_path / "missing.zip")


def test_multiple_clinicorp_candidates_require_human_selection(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    patients = [
        Patient(id=990000011, nome=PATIENT_NAME),
        Patient(id=990000012, nome=PATIENT_NAME),
    ]
    importer, _, _, output = build_importer(
        tmp_path,
        answers=("2", "1", "CONFIRMAR"),
        patients=patients,
    )

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["masked_patient_id"] == mask_patient_id(990000012)
    assert any("990000011" in line for line in output)
    assert any("990000012" in line for line in output)


def test_offline_source_requires_manual_patient_confirmation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path,
        answers=("CONFIRMAR", "1", "CONFIRMAR"),
    )
    importer.patient_repository = EmptyPatientRepository()

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["masked_patient_id"] is None
    assert result.destination.is_dir()


def test_multiple_onedrive_folders_require_human_selection(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    folders = (
        f"001 - {PATIENT_NAME}",
        f"002 - {PATIENT_NAME}",
    )
    importer, patients_root, _, output = build_importer(
        tmp_path,
        answers=("1", "2", "CONFIRMAR"),
        patient_folders=folders,
    )

    result = importer.run(archive_path=archive)

    assert result.destination.is_relative_to(patients_root / folders[1])
    assert any(folders[0] in line for line in output)
    assert any(folders[1] in line for line in output)


def test_no_onedrive_folder_stops_before_destination_creation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path,
        answers=("1",),
        patient_folders=(),
    )

    with pytest.raises(SupervisedImportError, match="Nenhuma pasta"):
        importer.run(archive_path=archive)

    assert list(patients_root.iterdir()) == []


def test_zip_path_traversal_is_rejected_without_external_write(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / "PACIENTE FICTICIO.zip",
        {"../../outside.txt": b"proibido"},
    )
    extractor = ArchiveExtractor(
        tmp_path / "quarantine",
        tmp_path / "WinRAR.exe",
    )

    with pytest.raises(ArchiveExtractionError, match="caminho inseguro"):
        extractor.extract(archive)

    assert not (tmp_path / "outside.txt").exists()
    assert archive.exists()


def test_folder_outside_configured_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "patients"
    root.mkdir()
    outside = tmp_path / "outside-patient"
    outside.mkdir()
    locator = PatientFolderLocator(root)

    with pytest.raises(PatientFolderError, match="fora da raiz"):
        locator.validate_selection(outside)


def test_copy_destination_outside_root_is_rejected_before_creation(
    tmp_path: Path,
) -> None:
    importer, _, _, _ = build_importer(tmp_path)
    extracted = tmp_path / "extracted-outside-check"
    extracted.mkdir()
    source_file = extracted / "scan.bin"
    source_file.write_bytes(b"conteudo")
    outside = tmp_path / "outside" / "destination"

    with pytest.raises(SupervisedImportError, match="fora da raiz"):
        importer._copy_and_manifest(
            archive=tmp_path / "archive.zip",
            extracted=extracted,
            destination=outside,
            patient=ConfirmedPatient(PATIENT_NAME, 990000010),
            files=[source_file],
            total_size=8,
            source="local",
        )

    assert not outside.exists()


def test_existing_destination_gets_incremental_suffix(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(tmp_path)
    existing = (
        patients_root
        / PATIENT_NAME
        / "Exames de imagem"
        / "2099-12-31 - Radiologia"
    )
    existing.mkdir(parents=True)
    marker = existing / "existing.txt"
    marker.write_text("não sobrescrever", encoding="utf-8")

    result = importer.run(archive_path=archive)

    assert result.destination.name == "2099-12-31 - Radiologia (2)"
    assert marker.read_text(encoding="utf-8") == "não sobrescrever"


def test_preview_reports_possible_duplicate_before_copy(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"scan.bin": b"mesmo-tamanho"},
    )
    importer, patients_root, _, output = build_importer(tmp_path)
    previous = (
        patients_root
        / PATIENT_NAME
        / "Exames de imagem"
        / "2099-12-30 - Radiologia"
    )
    previous.mkdir(parents=True)
    (previous / "scan.bin").write_bytes(b"outro-tamanho")

    importer.run(archive_path=archive)

    assert "Possíveis duplicados: 1" in output


def test_user_without_exact_confirmation_causes_no_copy(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, quarantine, _ = build_importer(
        tmp_path,
        answers=("1", "1", "confirmar"),
    )

    with pytest.raises(SupervisedImportCancelled, match="confirmação explícita"):
        importer.run(archive_path=archive)

    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()
    assert archive.exists()
    assert any(path.is_dir() for path in quarantine.iterdir())


def test_copy_refuses_destination_created_after_preview(tmp_path: Path) -> None:
    importer, patients_root, _, _ = build_importer(tmp_path)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    source_file = extracted / "scan.bin"
    source_file.write_bytes(b"novo")
    destination = patients_root / PATIENT_NAME / "existing-destination"
    destination.mkdir()
    existing = destination / "scan.bin"
    existing.write_bytes(b"antigo")

    with pytest.raises(SupervisedImportError, match="nenhuma sobrescrita"):
        importer._copy_and_manifest(
            archive=tmp_path / "archive.zip",
            extracted=extracted,
            destination=destination,
            patient=ConfirmedPatient(PATIENT_NAME, 990000010),
            files=[source_file],
            total_size=4,
            source="local",
        )

    assert existing.read_bytes() == b"antigo"


def test_email_mode_uses_readonly_message_and_injected_download(tmp_path: Path) -> None:
    message = EmailMessage(
        message_id="fixture-message-id",
        subject=f'TransferNow - "{PATIENT_NAME}_20991231.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to=None,
        received_at=datetime(2099, 12, 31, tzinfo=timezone.utc),
        html_body=(
            '<a href="https://transfernow.net/dl/secret-fixture-token">'
            "Baixar</a>"
        ),
    )

    class ReadOnlyGmail:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def get_message(self, message_id: str) -> EmailMessage:
            self.ids.append(message_id)
            return message

    gmail = ReadOnlyGmail()
    downloads: list[tuple[str, str, Path]] = []

    def fake_download(url: str, filename: str, quarantine: Path) -> Path:
        downloads.append((url, filename, quarantine))
        quarantine.mkdir(parents=True, exist_ok=True)
        return create_zip(quarantine / filename)

    importer, _, _, _ = build_importer(
        tmp_path,
        gmail_connector=gmail,
        downloader=fake_download,
    )

    result = importer.run(email_message_id="fixture-message-id")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert gmail.ids == ["fixture-message-id"]
    assert len(downloads) == 1
    assert manifest["source"] == "gmail"
    assert "secret-fixture-token" not in result.manifest_path.read_text(
        encoding="utf-8"
    )


def test_default_downloader_failure_is_sanitized(tmp_path: Path, monkeypatch) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)

    def connection_failure(*args, **kwargs):
        raise requests.ConnectionError(
            "https://transfernow.net/dl/SECRET-TOKEN"
        )

    monkeypatch.setattr(requests, "get", connection_failure)

    with pytest.raises(SupervisedImportError) as captured:
        importer._download_transfernow(
            "https://transfernow.net/dl/SECRET-TOKEN",
            "PACIENTE FICTICIO.zip",
            quarantine,
        )

    assert "SECRET-TOKEN" not in str(captured.value)


def test_default_downloader_writes_only_inside_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            assert chunk_size == 1024 * 1024
            return iter((b"parte-1", b"", b"parte-2"))

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    downloaded = importer._download_transfernow(
        "https://transfernow.net/dl/fixture",
        "EXAME_FICTICIO.zip",
        quarantine,
    )

    assert downloaded.parent == quarantine.resolve()
    assert downloaded.read_bytes() == b"parte-1parte-2"
    assert calls == [
        (
            "https://transfernow.net/dl/fixture",
            {"stream": True, "timeout": 60},
        )
    ]


def test_default_downloader_rejects_non_transfernow_url(tmp_path: Path) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)

    with pytest.raises(SupervisedImportError, match="não é permitido"):
        importer._download_transfernow(
            "https://eviltransfernow.example/dl/token",
            "EXAME_FICTICIO.zip",
            quarantine,
        )

    assert not quarantine.exists()


def test_successful_flow_never_calls_delete_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)

    def forbidden_delete(*args, **kwargs):
        raise AssertionError("Nenhuma exclusão é permitida")

    monkeypatch.setattr(Path, "unlink", forbidden_delete)
    monkeypatch.setattr(Path, "rmdir", forbidden_delete)
    monkeypatch.setattr(shutil, "rmtree", forbidden_delete)

    result = importer.run(archive_path=archive)

    assert result.manifest_path.is_file()
    assert archive.exists()


def test_cli_dispatches_supervised_archive_mode_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import main
    from radiology import supervised_import

    calls = {}

    class FakeImporter:
        def __init__(self, **kwargs) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs):
            calls["run"] = kwargs
            return SimpleNamespace(
                destination=tmp_path / "destination",
                manifest_path=tmp_path / "destination" / "manifest.json",
            )

    monkeypatch.setattr(
        supervised_import,
        "SupervisedRadiologyImporter",
        FakeImporter,
    )

    result = main.main(
        [
            "radiology-import-supervised",
            "--archive-path",
            str(tmp_path / "fixture.zip"),
            "--patient-source",
            "offline",
        ]
    )

    assert result == 0
    assert isinstance(
        calls["init"]["patient_repository"],
        EmptyPatientRepository,
    )
    assert calls["init"]["gmail_connector"] is None
    assert calls["run"] == {
        "archive_path": str(tmp_path / "fixture.zip"),
        "email_message_id": None,
    }


def test_cli_requires_exactly_one_input_option() -> None:
    import main

    assert main.main(["radiology-import-supervised"]) == 2
    assert main.main(
        [
            "radiology-import-supervised",
            "--archive-path",
            "fixture.zip",
            "--email-message-id",
            "fixture-message",
        ]
    ) == 2

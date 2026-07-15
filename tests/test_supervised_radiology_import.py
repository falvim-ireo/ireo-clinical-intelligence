"""Testes totalmente offline do MVP supervisionado de radiologia."""

from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
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
from models.resolved_patient import ResolvedPatient, ResolutionReason
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
from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
from repositories.patient_repository import InMemoryPatientRepository
from repositories.patient_repository import EmptyPatientRepository
from repositories.patient_repository import PatientRepositoryUnavailableError


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
    auto_select_unambiguous: bool = False,
    auto_select_min_score: float = 0.95,
    force_manual_selection: bool = False,
    patient_source_mode: str = "clinicorp",
    audit_logger=None,
    intake_history=None,
    allow_reimport: bool = False,
) -> tuple[SupervisedRadiologyImporter, Path, Path, list[str]]:
    patients_root = tmp_path / "patients"
    patients_root.mkdir(exist_ok=True)
    for folder_name in patient_folders:
        (patients_root / folder_name).mkdir(exist_ok=True)
    quarantine = tmp_path / "quarantine"
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"ferramenta-ficticia")
    output: list[str] = []
    audit = audit_logger or AuditLogger(correlation_id="correlation-supervised-0001")
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
        auto_select_unambiguous=auto_select_unambiguous,
        auto_select_min_score=auto_select_min_score,
        force_manual_selection=force_manual_selection,
        patient_source_mode=patient_source_mode,
        intake_history=intake_history,
        allow_reimport=allow_reimport,
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


def test_local_rar_uses_unrar_without_shell_and_keeps_inputs(
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
            listing = "scan/image.dcm\n"
            return subprocess.CompletedProcess(command, 0, listing, "")
        destination = Path(command[-1].rstrip("\\"))
        (destination / "scan").mkdir(parents=True, exist_ok=True)
        (destination / "scan" / "image.dcm").write_bytes(b"rar-extraido")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = importer.run(archive_path=archive)

    assert archive.exists()
    assert result.file_count == 1
    assert len(calls) == 2
    assert calls[0][0][0].endswith("UnRAR.exe")
    assert calls[0][0][1:] == ["lb", str(archive.resolve())]
    assert calls[1][0][0].endswith("UnRAR.exe")
    assert calls[1][0][1:3] == ["x", "-o-"]
    assert calls[1][0][-2] == str(archive.resolve())
    assert calls[1][0][-1].endswith("\\")
    assert all("WinRAR.exe" not in argument for call, _ in calls for argument in call)
    assert all(call_kwargs["shell"] is False for _, call_kwargs in calls)
    assert all(call_kwargs["timeout"] == 1800 for _, call_kwargs in calls)
    assert all(call_kwargs["capture_output"] is True for _, call_kwargs in calls)
    assert all(
        call_kwargs["creationflags"]
        == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for _, call_kwargs in calls
    )


def test_unrar_not_found_is_safe(tmp_path: Path) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    extractor = ArchiveExtractor(
        tmp_path / "quarantine",
        tmp_path / "missing-unrar.exe",
    )

    with pytest.raises(ArchiveExtractionError, match="não foi encontrado"):
        extractor.extract(archive)

    assert archive.exists()


def test_unrar_failure_is_checked(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "secret"),
    )

    with pytest.raises(ArchiveExtractionError, match="não conseguiu"):
        extractor.extract(archive)


def test_unrar_timeout_is_safe(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool, timeout_seconds=1)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output="sensitive")

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ArchiveExtractionError, match="tempo limite"):
        extractor.extract(archive)


def test_unrar_listing_blocks_traversal_before_extraction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        listing = "../../outside.txt\n"
        return subprocess.CompletedProcess(command, 0, listing, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)

    with pytest.raises(ArchiveExtractionError, match="caminho inseguro"):
        extractor.extract(archive)

    assert len(calls) == 1
    assert calls[0][1] == "lb"
    assert not (tmp_path / "outside.txt").exists()


def test_unrar_lb_accepts_large_real_pilot_style_listing() -> None:
    listing = "\n".join(
        f"VERA LUCIA CRUZ/CT/SERIE_{index:04d}/IMAGEM_{index:04d}.dcm"
        for index in range(1200)
    )
    listing = f"\n{listing}\n\n"

    members = ArchiveExtractor._parse_unrar_listing(listing)

    assert len(members) == 1200
    assert members[0] == "VERA LUCIA CRUZ/CT/SERIE_0000/IMAGEM_0000.dcm"
    assert members[-1] == "VERA LUCIA CRUZ/CT/SERIE_1199/IMAGEM_1199.dcm"


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
        tmp_path / "UnRAR.exe",
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


def test_copy_confirmation_is_case_insensitive_and_ignores_spaces(
    tmp_path: Path,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path,
        answers=("1", "1", "  confirmar  "),
    )

    result = importer.run(archive_path=archive)

    assert result.destination.is_dir()


def test_enter_cancels_copy_with_friendly_prompt(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    prompts: list[str] = []
    answers = iter(("1", "1", ""))
    importer, patients_root, quarantine, _ = build_importer(tmp_path)
    importer.input = lambda prompt: (prompts.append(prompt), next(answers))[1]

    with pytest.raises(SupervisedImportCancelled, match="confirmação explícita"):
        importer.run(archive_path=archive)

    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()
    assert archive.exists()
    assert any(path.is_dir() for path in quarantine.iterdir())
    assert "Digite CONFIRMAR (não diferencia maiúsculas/minúsculas)" in prompts[-1]
    assert "[ENTER] = cancelar" in prompts[-1]
    assert "CONFIRMAR = copiar" in prompts[-1]


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


def resolved_fixture(
    *, score: float = 1.0, reason=ResolutionReason.EXACT_NAME,
    patient_id: int | None = 990000010, candidate_count: int = 1,
    matched: bool = True, manual: bool = False,
) -> ResolvedPatient:
    return ResolvedPatient(
        patient_id=patient_id,
        patient_name=PATIENT_NAME if matched else None,
        matched=matched,
        requires_manual_review=manual,
        confidence_score=score,
        resolution_reason=reason,
        candidate_count=candidate_count,
        candidate_names=[PATIENT_NAME] * candidate_count,
        matched_by="exact_name" if matched else None,
    )


def test_auto_selection_flag_false_preserves_manual_flow(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, output = build_importer(tmp_path)

    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))

    assert manifest["selection_mode"] == {"patient": "manual", "folder": "manual"}
    assert any("Candidatos de paciente" in line for line in output)


def test_exact_patient_and_single_folder_are_auto_selected(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, output = build_importer(
        tmp_path, answers=("CONFIRMAR",), auto_select_unambiguous=True,
    )

    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))

    assert manifest["selection_mode"] == {"patient": "auto", "folder": "auto"}
    assert manifest["auto_selection_reason"] == {
        "patient": "EXACT_NAME", "folder": "SINGLE_COMPATIBLE_FOLDER"
    }
    assert any("Paciente selecionado automaticamente" in line for line in output)
    assert any("Pasta selecionada automaticamente" in line for line in output)


@pytest.mark.parametrize("score, expected", [(0.94, False), (0.95, True)])
def test_auto_selection_uses_independent_inclusive_threshold(
    tmp_path: Path, score: float, expected: bool,
) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    assert importer._is_patient_auto_selectable(resolved_fixture(score=score)) is expected


@pytest.mark.parametrize(
    "resolved",
    [
        resolved_fixture(candidate_count=2, manual=True, matched=False,
                         reason=ResolutionReason.MULTIPLE_HIGH_SCORE),
        resolved_fixture(candidate_count=1, manual=True, matched=False,
                         reason=ResolutionReason.MULTIPLE_HIGH_SCORE),
        resolved_fixture(patient_id=None),
        resolved_fixture(reason="FUTURE_UNKNOWN_REASON"),
    ],
)
def test_unsafe_patient_resolution_never_auto_selects(
    tmp_path: Path, resolved: ResolvedPatient,
) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    assert importer._is_patient_auto_selectable(resolved) is False


def test_patient_source_unavailable_stops_safely(tmp_path: Path) -> None:
    class UnavailableRepository:
        def find_candidates(self, name):
            raise PatientRepositoryUnavailableError("sensitive")

    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True,
    )
    importer.patient_repository = UnavailableRepository()

    with pytest.raises(SupervisedImportError, match="indisponível"):
        importer.run(archive_path=archive)
    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()


def test_auto_folder_outside_root_is_rejected(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    importer.folder_locator.find_compatible = lambda name: [outside]

    with pytest.raises(PatientFolderError, match="fora da raiz"):
        importer._choose_patient_folder(PATIENT_NAME, allow_auto=True)


def test_two_folders_remain_manual_when_feature_is_active(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    folders = (f"001 - {PATIENT_NAME}", f"002 - {PATIENT_NAME}")
    importer, patients_root, _, _ = build_importer(
        tmp_path, answers=("2", "CONFIRMAR"), patient_folders=folders,
        auto_select_unambiguous=True,
    )

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert result.destination.is_relative_to(patients_root / folders[1])
    assert manifest["selection_mode"] == {"patient": "auto", "folder": "manual"}


def test_incoherent_folder_name_is_not_selected(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, patient_folders=("OUTRO PACIENTE",), auto_select_unambiguous=True,
    )
    with pytest.raises(SupervisedImportError, match="Nenhuma pasta"):
        importer.run(archive_path=archive)


def test_force_manual_override_disables_both_auto_selections(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True, force_manual_selection=True,
    )
    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))
    assert manifest["selection_mode"] == {"patient": "manual", "folder": "manual"}


def test_auto_selection_still_requires_final_confirmation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path, answers=("",), auto_select_unambiguous=True,
    )
    with pytest.raises(SupervisedImportCancelled, match="confirmação explícita"):
        importer.run(archive_path=archive)
    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()


def test_selection_audit_is_sanitized(tmp_path: Path) -> None:
    stream = io.StringIO()
    logger = logging.Logger("selection-audit")
    logger.addHandler(logging.StreamHandler(stream))
    audit = AuditLogger(logger=logger, level="INFO", correlation_id="correlation-auto-0001")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, answers=("CONFIRMAR",), auto_select_unambiguous=True,
        audit_logger=audit,
    )

    importer.run(archive_path=archive)
    payload = stream.getvalue()
    assert "PATIENT_AUTO_SELECTED" in payload
    assert "ONEDRIVE_FOLDER_AUTO_SELECTED" in payload
    assert '"confidence_score":1.0' in payload
    assert '"candidate_count":1' in payload
    assert '"folder_candidate_count":1' in payload
    assert '"override_manual":false' in payload
    assert PATIENT_NAME not in payload
    assert str(tmp_path) not in payload


def test_offline_mode_never_auto_selects(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True, patient_source_mode="offline",
    )
    assert importer._is_patient_auto_selectable(resolved_fixture()) is False


def test_cli_force_manual_option_reaches_importer(tmp_path: Path, monkeypatch) -> None:
    import main
    from radiology import supervised_import

    calls = {}

    class FakeImporter:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def run(self, **kwargs):
            return SimpleNamespace(
                destination=tmp_path / "destination",
                manifest_path=tmp_path / "destination" / "manifest.json",
            )

    monkeypatch.setattr(supervised_import, "SupervisedRadiologyImporter", FakeImporter)
    result = main.main([
        "radiology-import-supervised", "--archive-path", "fixture.zip",
        "--patient-source", "offline", "--force-manual-selection",
    ])
    assert result == 0
    assert calls["force_manual_selection"] is True


def test_completed_archive_is_blocked_by_default(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, patients_root, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, output = build_importer(tmp_path, intake_history=history)

    with pytest.raises(SupervisedImportError, match="bloqueada por padrão"):
        second.run(archive_path=archive)

    assert any("já foi importado" in line for line in output)
    assert len(list((patients_root / PATIENT_NAME / "Exames de imagem").iterdir())) == 1
    assert history.list_records(limit=1)[0].status == "DUPLICATE_DETECTED"


def test_allow_reimport_without_exact_word_does_not_copy(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, patients_root, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(
        tmp_path, answers=("",), intake_history=history, allow_reimport=True,
    )

    with pytest.raises(SupervisedImportCancelled, match="Reimportação cancelada"):
        second.run(archive_path=archive)
    assert len(list((patients_root / PATIENT_NAME / "Exames de imagem").iterdir())) == 1


def test_allow_reimport_with_two_explicit_confirmations_copies(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, _, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(
        tmp_path, answers=("REIMPORTAR", "1", "1", "CONFIRMAR"),
        intake_history=history, allow_reimport=True,
    )

    result = second.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert manifest["intake_record"]["reimport"] is True
    assert manifest["intake_record"]["duplicate_check"] == "confirmed"
    assert manifest["intake_record"]["previous_record_reference"].startswith("intake-")
    completed = history.list_records(status="COMPLETED")
    assert len(completed) == 2
    assert completed[0].reimport_confirmed == 1
    assert completed[0].previous_record_reference.startswith("intake-")


def test_history_failure_blocks_before_copy(tmp_path: Path) -> None:
    class UnavailableHistory:
        def create_downloaded(self, **kwargs):
            raise IntakeHistoryError("sensitive database path")

    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path, intake_history=UnavailableHistory(),
    )
    with pytest.raises(SupervisedImportError, match="histórico local"):
        importer.run(archive_path=archive)
    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()


def test_intake_manifest_preserves_file_checksums(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path, intake_history=history)
    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert manifest["checksums"] == result.checksums
    assert manifest["intake_record"]["archive_sha256"] == importer._file_sha256(archive)
    assert manifest["intake_record"]["duplicate_check"] == "clear"


def test_duplicate_audit_contains_no_clinical_values(tmp_path: Path) -> None:
    stream = io.StringIO()
    logger = logging.Logger("duplicate-audit")
    logger.addHandler(logging.StreamHandler(stream))
    audit = AuditLogger(logger=logger, level="INFO", correlation_id="correlation-history-1")
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, _, _, _ = build_importer(tmp_path, intake_history=history, audit_logger=audit)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(tmp_path, intake_history=history, audit_logger=audit)
    with pytest.raises(SupervisedImportError):
        second.run(archive_path=archive)
    payload = stream.getvalue()
    assert "INTAKE_RECORD_CREATED" in payload
    assert "DUPLICATE_CONFIRMED" in payload
    assert PATIENT_NAME not in payload
    assert str(tmp_path) not in payload

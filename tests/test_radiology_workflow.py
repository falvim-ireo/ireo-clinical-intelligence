"""Testes offline da orquestração radiológica."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from models.patient import Patient
from radiology.dicom_reader import DicomStudy
from radiology.patient_matcher import MatchStatus, PatientMatchResult
from radiology.transfernow_download import DownloadResult, TransferNowDownloadError
from radiology.zip_extractor import ExtractionResult, ZipExtractionError
from workflows.radiology_workflow import RadiologyWorkflow, WorkflowResult


def study() -> DicomStudy:
    """Cria um estudo mínimo usado pelos doubles dos testes."""

    return DicomStudy(
        patient_name="PACIENTE TESTE",
        patient_id="1",
        study_date="20260721",
        study_description=None,
        study_instance_uid="1.2.3",
        manufacturer=None,
        manufacturer_model_name=None,
        institution_name="IREO",
        modality="CT",
        series=[],
    )


class FakeDownloader:
    """Double que registra a etapa de download."""

    def __init__(self, result: DownloadResult, calls: list[str]) -> None:
        self.result = result
        self.calls = calls

    def download(self, *_args) -> DownloadResult:
        self.calls.append("download")
        return self.result


class FakeExtractor:
    """Double que registra a etapa de extração."""

    def __init__(self, result: ExtractionResult, calls: list[str]) -> None:
        self.result = result
        self.calls = calls

    def extract(self, _path: Path) -> ExtractionResult:
        self.calls.append("extract")
        return self.result


class FakeReader:
    """Double que registra a etapa de leitura DICOM."""

    def __init__(self, result: DicomStudy, calls: list[str]) -> None:
        self.result = result
        self.calls = calls

    def read(self, _extraction: ExtractionResult) -> DicomStudy:
        self.calls.append("read")
        return self.result


class FakeMatcher:
    """Double que registra a associação do paciente."""

    def __init__(self, result: PatientMatchResult, calls: list[str]) -> None:
        self.result = result
        self.calls = calls

    def match(
        self, _study: DicomStudy, _patients: list[Patient]
    ) -> PatientMatchResult:
        self.calls.append("match")
        return self.result


def run_workflow(workflow: RadiologyWorkflow, tmp_path: Path) -> WorkflowResult:
    """Executa o workflow com argumentos locais e determinísticos."""

    return workflow.run(
        transfer_url="https://transfernow.net/dl/fixture",
        original_filename="exam.zip",
        download_root=tmp_path,
        correlation_id="correlation-0001",
        message_id="message-0001",
        patients=[Patient(id=1, nome="PACIENTE TESTE")],
    )


def test_orchestrates_all_steps_and_returns_artifacts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    archive = tmp_path / "exam.zip"
    archive.write_bytes(b"fixture")
    download = DownloadResult(archive, 7, "digest")
    extraction = ExtractionResult(tmp_path / "extracted", [], None)
    dicom_study = study()
    match = PatientMatchResult(
        MatchStatus.MATCHED,
        1.0,
        (),
        Patient(id=1, nome="PACIENTE TESTE"),
        ("PatientID exato e único.",),
    )
    workflow = RadiologyWorkflow(
        FakeDownloader(download, calls),
        FakeExtractor(extraction, calls),
        FakeReader(dicom_study, calls),
        FakeMatcher(match, calls),
    )

    with caplog.at_level(logging.INFO):
        result = run_workflow(workflow, tmp_path)

    assert calls == ["download", "extract", "read", "match"]
    assert result.success is True
    assert result.download is download
    assert result.extraction is extraction
    assert result.study is dicom_study
    assert result.patient_match is match
    assert result.errors == []
    assert result.duration_seconds >= 0
    assert "Workflow radiológico finalizado" in caplog.text


@pytest.mark.parametrize(
    ("failed_stage", "stage_label", "exception", "expected_calls"),
    [
        (
            "download",
            "download",
            TransferNowDownloadError("download seguro"),
            ["download"],
        ),
        (
            "extract",
            "extração ZIP",
            ZipExtractionError("zip seguro"),
            ["download", "extract"],
        ),
        (
            "read",
            "leitura DICOM",
            RuntimeError("sensitive detail"),
            ["download", "extract", "read"],
        ),
        (
            "match",
            "associação de paciente",
            RuntimeError("sensitive detail"),
            ["download", "extract", "read", "match"],
        ),
    ],
)
def test_never_raises_and_returns_stage_error(
    tmp_path: Path,
    failed_stage: str,
    stage_label: str,
    exception: Exception,
    expected_calls: list[str],
) -> None:
    calls: list[str] = []
    archive = tmp_path / "exam.zip"
    archive.write_bytes(b"fixture")
    download = DownloadResult(archive, 7, "digest")
    extraction = ExtractionResult(tmp_path / "extracted", [], None)
    dicom_study = study()
    match = PatientMatchResult(MatchStatus.NO_MATCH, 0.0, (), None, ("none",))

    class FailingStep:
        def __init__(self, stage: str, result) -> None:
            self.stage = stage
            self.result = result

        def _call(self, name: str):
            calls.append(name)
            if failed_stage == self.stage:
                raise exception
            return self.result

        def download(self, *_args):
            return self._call("download")

        def extract(self, _path):
            return self._call("extract")

        def read(self, _source):
            return self._call("read")

        def match(self, _study, _patients):
            return self._call("match")

    workflow = RadiologyWorkflow(
        FailingStep("download", download),
        FailingStep("extract", extraction),
        FailingStep("read", dicom_study),
        FailingStep("match", match),
    )

    result = run_workflow(workflow, tmp_path)

    assert result.success is False
    assert calls == expected_calls
    assert len(result.errors) == 1
    assert stage_label in result.errors[0]
    if isinstance(exception, RuntimeError) and not isinstance(
        exception, (TransferNowDownloadError, ZipExtractionError)
    ):
        assert "sensitive detail" not in result.errors[0]
    assert result.duration_seconds >= 0

import importlib

import pytest

from integrations.transfernow_connector import TransferNowConnector
from models.imaging_exam import ImagingExam


def test_imaging_exam_can_be_imported_and_marked_as_imported() -> None:
    exam = ImagingExam(patient_name="Paciente de Teste")

    exam.mark_imported()

    assert exam.is_imported is True
    assert exam.imported_at is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://transfernow.net/dl/example",
        "https://www.transfernow.net/dl/example",
        "https://files.transfernow.net/dl/example?token=abc",
    ],
)
def test_transfernow_accepts_https_official_hosts(url: str) -> None:
    message = TransferNowConnector.interpretar(url)

    assert message.download_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://transfernow.net/dl/insecure",
        "https://eviltransfernow.net/dl/example",
        "https://transfernow.net.evil.example/dl/example",
    ],
)
def test_transfernow_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Nenhum link válido"):
        TransferNowConnector.interpretar(url)


def test_transfernow_extracts_link_from_html_href() -> None:
    html = (
        '<p>Seu exame está disponível.</p>'
        '<a href="https://transfernow.net/dl/example?foo=1&amp;bar=2">'
        "Baixar arquivo"
        "</a>"
    )

    message = TransferNowConnector.interpretar(html)

    assert (
        message.download_url
        == "https://transfernow.net/dl/example?foo=1&bar=2"
    )


def test_configuration_is_centralized_without_output(capsys) -> None:
    from api import clinicorp_connector
    from core.config import Config

    expected_settings = {
        "CLINICORP_SUBSCRIBER_ID",
        "CLINICORP_BUSINESS_ID",
        "CLINICORP_API_USER",
        "CLINICORP_TOKEN",
        "CLINICORP_BASE_URL",
    }

    assert expected_settings.issubset(vars(Config))
    assert clinicorp_connector.Config is Config
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "module_name",
    [
        "main",
        "api.clinicorp_connector",
        "core.config",
        "integrations.gmail_connector",
        "integrations.onedrive_connector",
        "integrations.transfernow_connector",
        "intelligence.maintenance_engine",
        "models.appointment",
        "models.email_message",
        "models.imaging_exam",
        "models.patient",
        "models.radiology_intake_plan",
        "radiology.archive_extractor",
        "radiology.dicom_reader",
        "radiology.patient_matcher",
        "services.appointment_service",
        "services.patient_service",
        "services.radiology_import_service",
        "utils.formatters",
        "utils.logger",
        "workflows.imaging_workflow",
        "workflows.maintenance_workflow",
    ],
)
def test_main_modules_are_importable(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None

"""Contrato offline ClinicorpAPI -> repositório -> resolvedor."""

import json
from pathlib import Path
import socket
from typing import Any

import pytest
import requests

from api.clinicorp_connector import ClinicorpAPI
from models.imaging_exam import ImagingExam
from models.resolved_patient import ResolutionReason
from repositories.clinicorp_patient_repository import (
    ClinicorpPatientRepository,
)
from repositories.patient_repository import (
    PatientRepositoryUnavailableError,
)
from services.patient_resolver import PatientResolver


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "clinicorp"


@pytest.fixture(autouse=True)
def block_all_network(monkeypatch) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Acesso real à rede proibido no contrato")

    monkeypatch.setattr(requests, "get", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)


def load_fixture(name: str) -> Any:
    return json.loads(
        (FIXTURE_DIR / name).read_text(encoding="utf-8")
    )


class FixtureClinicorpAPI(ClinicorpAPI):
    """Exercita o método real sem construir autenticação ou fazer rede."""

    def __init__(self, response: Any = None, error: Exception | None = None):
        self.subscriber_id = "fixture-subscriber"
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _get(self, caminho: str, params: dict[str, Any]) -> Any:
        self.calls.append((caminho, params))
        if self.error is not None:
            raise self.error
        return self.response


def repository_for(name: str) -> tuple[ClinicorpPatientRepository, FixtureClinicorpAPI]:
    api = FixtureClinicorpAPI(load_fixture(name))
    return ClinicorpPatientRepository(api), api


def test_active_fixture_converts_to_patient_and_preserves_contract() -> None:
    repository, api = repository_for("active_valid.json")

    patients = repository.find_candidates("Paciente Fictício Alfa")

    assert len(patients) == 1
    assert patients[0].id == 990000001
    assert patients[0].nome == "PACIENTE FICTÍCIO ALFA"
    assert patients[0].status == "ACTIVE"
    assert api.calls == [
        (
            "patient/get",
            {
                "subscriber_id": "fixture-subscriber",
                "Name": "Paciente Fictício Alfa",
            },
        )
    ]


def test_deleted_fixture_is_excluded() -> None:
    repository, _ = repository_for("deleted_patient.json")

    assert repository.find_candidates("Paciente Fictício Excluído") == []


def test_duplicate_id_is_removed_and_response_order_is_preserved() -> None:
    repository, _ = repository_for("duplicate_patient_id.json")

    patients = repository.find_candidates("Paciente Fictício")

    assert [(patient.id, patient.nome) for patient in patients] == [
        (990000005, "PACIENTE FICTÍCIO PRIMEIRO"),
        (990000006, "PACIENTE FICTÍCIO ÚLTIMO"),
    ]


@pytest.mark.parametrize("fixture_name", ["empty_object.json", "empty_list.json"])
def test_empty_contract_responses_are_safe(fixture_name: str) -> None:
    repository, _ = repository_for(fixture_name)

    assert repository.find_candidates("Paciente Fictício") == []


@pytest.mark.parametrize(
    "fixture_name",
    ["missing_patient_id.json", "missing_name.json"],
)
def test_missing_required_fields_are_discarded(fixture_name: str) -> None:
    repository, _ = repository_for(fixture_name)

    assert repository.find_candidates("Paciente Fictício") == []


def test_birth_date_is_preserved() -> None:
    repository, _ = repository_for("birth_date_present.json")

    patient = repository.find_candidates("Paciente Fictício Data")[0]

    assert patient.data_nascimento == "2099-12-31"


def test_contact_fields_are_preserved_in_domain_but_never_use_network() -> None:
    repository, _ = repository_for("contact_fields_present.json")

    patient = repository.find_candidates("Paciente Fictício Contato")[0]

    assert patient.telefone == "+00 00 00000-0000"
    assert patient.email == "fixture1@example.invalid"


def test_structurally_invalid_response_becomes_sanitized_domain_error() -> None:
    repository, _ = repository_for("structurally_invalid.json")

    with pytest.raises(PatientRepositoryUnavailableError) as captured:
        repository.find_candidates("Paciente Fictício")

    message = str(captured.value)
    assert message == "A fonte de pacientes está temporariamente indisponível."
    assert "RESPOSTA_FICTICIA_INVALIDA" not in message
    assert "Clinicorp" not in message


@pytest.mark.parametrize(
    ("external_error", "expected_message"),
    [
        (
            requests.exceptions.HTTPError(
                "401 token=PROIBIDO body=PROIBIDO"
            ),
            "A Clinicorp retornou um erro HTTP.",
        ),
        (
            requests.exceptions.ConnectionError(
                "api_user=PROIBIDO url=https://clinicorp.invalid/secret"
            ),
            "Não foi possível conectar à Clinicorp.",
        ),
    ],
)
def test_clinicorp_http_errors_do_not_expose_external_details(
    monkeypatch,
    external_error: Exception,
    expected_message: str,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            raise external_error

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse())
    api = object.__new__(ClinicorpAPI)
    api.base_url = "https://fixture-clinicorp.invalid"
    api.auth = None
    api.headers = {}

    with pytest.raises(RuntimeError) as captured:
        api._get("patient/get", {"Name": "PACIENTE FICTÍCIO"})

    message = str(captured.value)
    assert message == expected_message
    assert "PROIBIDO" not in message
    assert "401" not in message
    assert "/secret" not in message
    assert captured.value.__cause__ is None


def test_mixed_case_and_accents_are_preserved_and_resolved_normally() -> None:
    repository, api = repository_for("mixed_case_accents.json")
    resolver = PatientResolver(repository)

    result = resolver.resolve(
        ImagingExam(patient_name="CLÁUDIA EXEMPLO FICTICIA")
    )

    assert result.patient_id == 990000010
    assert result.patient_name == "Cláudia Exemplo Fictícia"
    assert result.resolution_reason is ResolutionReason.NORMALIZED_NAME
    assert result.requires_manual_review is False
    assert api.calls[0][1]["Name"] == "CLÁUDIA EXEMPLO FICTICIA"


def test_same_name_contract_fixture_requires_manual_review() -> None:
    repository, _ = repository_for("same_name_two_records.json")

    result = PatientResolver(repository).resolve(
        ImagingExam(patient_name="PACIENTE FICTÍCIO GÊMEO")
    )

    assert result.matched is False
    assert result.requires_manual_review is True
    assert result.patient_id is None
    assert result.resolution_reason is ResolutionReason.MULTIPLE_HIGH_SCORE
    assert result.candidate_count == 2


def test_source_unavailability_never_creates_automatic_association() -> None:
    api = FixtureClinicorpAPI(
        error=RuntimeError("token=PROIBIDO user=PROIBIDO body=PROIBIDO")
    )
    repository = ClinicorpPatientRepository(api)

    result = PatientResolver(repository).resolve(
        ImagingExam(patient_name="PACIENTE FICTÍCIO ALFA")
    )

    assert result.matched is False
    assert result.patient_id is None
    assert result.requires_manual_review is True
    assert result.resolution_reason is ResolutionReason.PATIENT_SOURCE_UNAVAILABLE


def test_all_fixtures_are_explicitly_synthetic() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FIXTURE_DIR.glob("*.json"))
    )
    lowered = fixture_text.casefold()

    assert "token" not in lowered
    assert "subscriber" not in lowered
    assert "business" not in lowered
    assert "clinicorp" not in lowered
    assert "@" not in fixture_text.replace("@example.invalid", "")

    for path in FIXTURE_DIR.glob("*.json"):
        payload = load_fixture(path.name)
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict):
                continue
            patient_id = record.get("PatientId")
            if patient_id is not None:
                assert str(patient_id).startswith("9900000")
            name = str(record.get("Name") or "").casefold()
            if name:
                assert "fict" in name
            email = record.get("Email")
            if email:
                assert str(email).endswith("@example.invalid")

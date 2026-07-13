from datetime import datetime, timezone
import socket

import pytest
import requests

from models.email_message import EmailMessage
from repositories.clinicorp_patient_repository import (
    ClinicorpPatientRepository,
)
from repositories.patient_repository import (
    PatientRepositoryUnavailableError,
)
from services.patient_normalizer import PatientNormalizer
from services.patient_resolver import PatientResolver
from workflows.imaging_workflow import ImagingWorkflow


@pytest.fixture(autouse=True)
def block_network(monkeypatch) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Acesso real a rede proibido")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)


class FakeClinicorpAPI:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = [] if response is None else response
        self.error = error
        self.calls = []

    def buscar_paciente(self, name: str, somente_ativos: bool = True):
        self.calls.append((name, somente_ativos))
        if self.error:
            raise self.error
        return self.response


def active_patient(patient_id=1, name="MARIA SILVA") -> dict:
    return {
        "PatientId": patient_id,
        "Name": name,
        "Status": "ACTIVE",
        "Phone": "0000-0000",
        "Email": "patient@example.test",
        "BirthDate": "2000-01-01",
    }


def radiology_message() -> EmailMessage:
    return EmailMessage(
        message_id="clinicorp-repository-workflow-001",
        subject='TransferNow - "MARIA SILVA_20260713.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to="Sorrimagem <contato@sorrimagem.example>",
        recipients=["radiologia@ireo.example"],
        received_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        text_body="Contato: contato@sorrimagem.example",
        html_body=(
            '<a href="https://transfernow.net/dl/test-token">Baixar</a>'
        ),
    )


def test_returns_one_valid_active_patient() -> None:
    api = FakeClinicorpAPI([active_patient(patient_id="101")])

    patients = ClinicorpPatientRepository(api).find_candidates("Maria Silva")

    assert len(patients) == 1
    assert patients[0].id == 101
    assert patients[0].nome == "MARIA SILVA"
    assert patients[0].status == "ACTIVE"
    assert patients[0].telefone == "0000-0000"
    assert patients[0].email == "patient@example.test"
    assert patients[0].data_nascimento == "2000-01-01"


def test_discards_deleted_patient_and_keeps_active_patient() -> None:
    deleted = active_patient(patient_id=102, name="MARIA DELETED")
    deleted["Status"] = "DELETED"
    api = FakeClinicorpAPI([deleted, active_patient(patient_id=103)])

    patients = ClinicorpPatientRepository(api).find_candidates("Maria Silva")

    assert [patient.id for patient in patients] == [103]


@pytest.mark.parametrize("response", [[], {}])
def test_empty_responses_return_empty_list(response) -> None:
    api = FakeClinicorpAPI(response)

    assert ClinicorpPatientRepository(api).find_candidates("Maria") == []


def test_discards_empty_and_malformed_items() -> None:
    api = FakeClinicorpAPI([{}, None, "invalid", active_patient()])

    patients = ClinicorpPatientRepository(api).find_candidates("Maria")

    assert [patient.id for patient in patients] == [1]


@pytest.mark.parametrize(
    "invalid_patient",
    [
        {"Name": "SEM ID", "Status": "ACTIVE"},
        {"PatientId": 104, "Status": "ACTIVE"},
        {"PatientId": "invalid", "Name": "ID RUIM", "Status": "ACTIVE"},
        {"PatientId": 105, "Name": "INATIVO", "Status": "INACTIVE"},
    ],
)
def test_discards_invalid_patient_records(invalid_patient: dict) -> None:
    api = FakeClinicorpAPI([invalid_patient])

    assert ClinicorpPatientRepository(api).find_candidates("Paciente") == []


def test_removes_duplicate_patient_ids_preserving_first_record() -> None:
    api = FakeClinicorpAPI(
        [
            active_patient(patient_id=106, name="PRIMEIRO NOME"),
            active_patient(patient_id="106", name="NOME DUPLICADO"),
        ]
    )

    patients = ClinicorpPatientRepository(api).find_candidates("Primeiro")

    assert [(patient.id, patient.nome) for patient in patients] == [
        (106, "PRIMEIRO NOME")
    ]


def test_preserves_order_of_multiple_active_patients() -> None:
    api = FakeClinicorpAPI(
        [
            active_patient(patient_id=109, name="TERCEIRO"),
            active_patient(patient_id=107, name="PRIMEIRO"),
            active_patient(patient_id=108, name="SEGUNDO"),
        ]
    )

    patients = ClinicorpPatientRepository(api).find_candidates("Paciente")

    assert [patient.id for patient in patients] == [109, 107, 108]


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("token=secret"),
        RuntimeError("401 user=private token=secret response=full-body"),
        RuntimeError("500 https://clinicorp.invalid?token=secret full-body"),
    ],
)
def test_external_failures_raise_sanitized_domain_error(error: Exception) -> None:
    api = FakeClinicorpAPI(error=error)

    with pytest.raises(PatientRepositoryUnavailableError) as captured:
        ClinicorpPatientRepository(api).find_candidates("Maria Silva")

    message = str(captured.value)
    assert message == "A fonte de pacientes está temporariamente indisponível."
    assert "secret" not in message
    assert "401" not in message
    assert "500" not in message
    assert "http" not in message
    assert api.calls == [("Maria Silva", True)]


@pytest.mark.parametrize("response", ["invalid", 123, {"unexpected": "data"}])
def test_structurally_invalid_response_raises_domain_error(response) -> None:
    api = FakeClinicorpAPI(response)

    with pytest.raises(
        PatientRepositoryUnavailableError,
        match="resposta inválida",
    ):
        ClinicorpPatientRepository(api).find_candidates("Maria")


def test_preserves_accents_and_case_in_clinicorp_query() -> None:
    api = FakeClinicorpAPI([])

    ClinicorpPatientRepository(api).find_candidates("Cláudia Cristina Kaiser")

    assert api.calls == [("Cláudia Cristina Kaiser", True)]


def test_strips_only_external_spaces_from_clinicorp_query() -> None:
    api = FakeClinicorpAPI([])

    ClinicorpPatientRepository(api).find_candidates(
        "  Cláudia Cristina Kaiser  "
    )

    assert api.calls == [("Cláudia Cristina Kaiser", True)]


def test_preserves_internal_spaces_and_word_order_in_query() -> None:
    api = FakeClinicorpAPI([])

    ClinicorpPatientRepository(api).find_candidates(
        "  Kaiser   Cláudia  Cristina  "
    )

    assert api.calls == [("Kaiser   Cláudia  Cristina", True)]


def test_normalizer_is_used_only_to_validate_returned_candidates(
    monkeypatch,
) -> None:
    api = FakeClinicorpAPI(
        [active_patient(name="CLÁUDIA CRISTINA KAISER")]
    )
    normalized_names = []

    def record_normalization(name: str) -> str:
        normalized_names.append(name)
        return "CLAUDIA CRISTINA KAISER"

    monkeypatch.setattr(
        PatientNormalizer,
        "compare_ready",
        staticmethod(record_normalization),
    )

    patients = ClinicorpPatientRepository(api).find_candidates(
        "Cláudia Cristina Kaiser"
    )

    assert [patient.id for patient in patients] == [1]
    assert api.calls == [("Cláudia Cristina Kaiser", True)]
    assert normalized_names == ["CLÁUDIA CRISTINA KAISER"]


@pytest.mark.parametrize("name", ["", "  \t\n  "])
def test_empty_name_returns_without_calling_clinicorp(name: str) -> None:
    api = FakeClinicorpAPI([])

    assert ClinicorpPatientRepository(api).find_candidates(name) == []
    assert api.calls == []


def test_workflow_converts_unavailability_to_manual_review() -> None:
    api = FakeClinicorpAPI(error=requests.exceptions.Timeout("sensitive"))
    repository = ClinicorpPatientRepository(api)
    workflow = ImagingWorkflow(
        patient_resolver=PatientResolver(repository)
    )
    plan = workflow.run_dry_run(radiology_message())

    assert plan.requires_manual_review is True
    assert plan.status == "DRY_RUN_REVIEW_REQUIRED"
    assert plan.review_reasons == [
        "Fonte de pacientes indisponível; revisão manual obrigatória."
    ]
    assert "/REVIEW_REQUIRED/" in plan.proposed_destination
    assert "sensitive" not in " ".join(plan.review_reasons)
    assert len(api.calls) == 1


def test_offline_composition_never_constructs_clinicorp(monkeypatch) -> None:
    import main

    def forbidden_clinicorp():
        raise AssertionError("Clinicorp não pode ser ativada no modo offline")

    monkeypatch.setattr(main, "ClinicorpAPI", forbidden_clinicorp)

    workflow = main.build_radiology_workflow("offline")
    plan = workflow.run_dry_run(radiology_message())

    assert plan.requires_manual_review is True
    assert plan.status == "DRY_RUN_REVIEW_REQUIRED"


def test_clinicorp_composition_uses_new_repository(monkeypatch) -> None:
    import main

    api = FakeClinicorpAPI([active_patient()])
    monkeypatch.setattr(main, "ClinicorpAPI", lambda: api)

    workflow = main.build_radiology_workflow("clinicorp")
    plan = workflow.run_dry_run(radiology_message())

    assert isinstance(
        workflow.patient_resolver.repository,
        ClinicorpPatientRepository,
    )
    assert plan.requires_manual_review is False
    assert api.calls == [("MARIA SILVA", True)]


def test_invalid_patient_source_is_rejected() -> None:
    import main

    with pytest.raises(ValueError, match="Fonte de pacientes inválida"):
        main.build_radiology_workflow("unknown")

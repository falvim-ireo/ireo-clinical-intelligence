"""Adapter somente leitura de candidatos da Clinicorp."""

from typing import Any

from api.clinicorp_connector import ClinicorpAPI
from models.patient import Patient
from repositories.patient_repository import PatientRepositoryUnavailableError
from services.patient_normalizer import PatientNormalizer


class ClinicorpPatientRepository:
    """Converte respostas Clinicorp válidas sem executar matching."""

    def __init__(self, api: ClinicorpAPI) -> None:
        self.api = api

    def find_candidates(self, name: str) -> list[Patient]:
        """Consulta uma vez pelo nome original e retorna pacientes ativos."""

        external_name = str(name or "").strip()
        if not external_name:
            return []

        try:
            raw_candidates = self.api.buscar_paciente(
                external_name,
                somente_ativos=True,
            )
        except Exception:
            raise PatientRepositoryUnavailableError(
                "A fonte de pacientes está temporariamente indisponível."
            ) from None

        if raw_candidates == {}:
            return []
        if not isinstance(raw_candidates, list):
            raise PatientRepositoryUnavailableError(
                "A fonte de pacientes retornou uma resposta inválida."
            )

        patients = []
        seen_ids = set()
        for item in raw_candidates:
            patient = self._to_active_patient(item)
            if patient is None or patient.id in seen_ids:
                continue
            seen_ids.add(patient.id)
            patients.append(patient)

        return patients

    @staticmethod
    def _to_active_patient(item: Any) -> Patient | None:
        if not isinstance(item, dict) or not item:
            return None

        status = str(item.get("Status") or "").strip().upper()
        patient_name = str(item.get("Name") or "").strip()
        patient_id = item.get("PatientId")
        comparable_name = PatientNormalizer.compare_ready(patient_name)
        if (
            status != "ACTIVE"
            or not comparable_name
            or patient_id in (None, "")
        ):
            return None

        try:
            normalized_id = int(patient_id)
        except (TypeError, ValueError):
            return None

        return Patient(
            id=normalized_id,
            nome=patient_name,
            telefone=item.get("Phone"),
            email=item.get("Email"),
            status=status,
            data_nascimento=item.get("BirthDate"),
        )

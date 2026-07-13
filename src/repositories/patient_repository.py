"""Porta de consulta de pacientes e implementação local para testes."""

from typing import Iterable, Protocol, Sequence

from models.patient import Patient


class PatientRepositoryUnavailableError(RuntimeError):
    """Indica que uma fonte de pacientes não pôde responder com segurança."""


class PatientRepository(Protocol):
    """Contrato mínimo para qualquer fonte de candidatos a paciente."""

    def find_candidates(self, name: str) -> Sequence[Patient]:
        """Retorna candidatos potencialmente relacionados ao nome."""


class EmptyPatientRepository:
    """Fonte segura usada quando nenhuma integração foi configurada."""

    def find_candidates(self, name: str) -> Sequence[Patient]:
        """Não resolve pacientes implicitamente sem uma fonte aprovada."""

        return ()


class InMemoryPatientRepository:
    """Fonte determinística em memória, sem acesso externo."""

    def __init__(self, patients: Iterable[Patient]) -> None:
        self._patients = tuple(patients)

    def find_candidates(self, name: str) -> Sequence[Patient]:
        """Retorna a coleção fornecida; o resolver concentra todo o matching."""

        return self._patients

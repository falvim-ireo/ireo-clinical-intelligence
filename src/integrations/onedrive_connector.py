"""Localização estritamente local de pastas de pacientes no OneDrive."""

from pathlib import Path

from services.patient_normalizer import PatientNormalizer


class PatientFolderError(RuntimeError):
    """Falha segura ao validar uma pasta local de paciente."""


class PatientFolderLocator:
    """Pesquisa somente diretórios filhos da raiz configurada."""

    def __init__(self, patients_root: str | Path) -> None:
        self.patients_root = Path(patients_root).expanduser().resolve()

    def find_compatible(self, patient_name: str) -> list[Path]:
        if not self.patients_root.is_dir():
            raise PatientFolderError(
                "A raiz local de pacientes do OneDrive não foi encontrada."
            )

        normalized_name = PatientNormalizer.compare_ready(patient_name)
        if not normalized_name:
            return []

        matches = []
        for candidate in sorted(
            self.patients_root.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.patients_root):
                continue
            normalized_folder = PatientNormalizer.compare_ready(candidate.name)
            if (
                normalized_folder == normalized_name
                or normalized_name in normalized_folder
            ):
                matches.append(resolved)
        return matches

    def validate_selection(self, patient_folder: str | Path) -> Path:
        selected = Path(patient_folder).expanduser().resolve()
        if (
            not selected.is_dir()
            or not selected.is_relative_to(self.patients_root)
            or selected == self.patients_root
        ):
            raise PatientFolderError(
                "A pasta selecionada está fora da raiz permitida."
            )
        return selected

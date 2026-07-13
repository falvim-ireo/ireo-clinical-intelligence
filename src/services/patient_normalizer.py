"""Normalização textual pura e reutilizável de nomes."""

import unicodedata
from typing import Optional


class PatientNormalizer:
    """Transforma nomes sem aplicar score, matching ou regras externas."""

    @staticmethod
    def remove_accents(name: Optional[str]) -> str:
        """Remove marcas diacríticas preservando letras e números."""

        decomposed = unicodedata.normalize("NFKD", str(name or ""))
        return "".join(
            character
            for character in decomposed
            if not unicodedata.combining(character)
        )

    @staticmethod
    def remove_special_characters(name: Optional[str]) -> str:
        """Substitui pontuação e símbolos por espaços."""

        return "".join(
            character
            if character.isalnum() or character.isspace()
            else " "
            for character in str(name or "")
        )

    @staticmethod
    def collapse_spaces(name: Optional[str]) -> str:
        """Converte sequências de espaços em um único espaço simples."""

        return " ".join(str(name or "").split())

    @staticmethod
    def normalize(name: Optional[str]) -> str:
        """Produz um nome maiúsculo, limpo e com espaços estáveis."""

        without_accents = PatientNormalizer.remove_accents(name)
        uppercase = without_accents.upper()
        with_dicom_spaces = uppercase.replace("^", " ")
        alphanumeric = PatientNormalizer.remove_special_characters(
            with_dicom_spaces
        )
        return PatientNormalizer.collapse_spaces(alphanumeric)

    @staticmethod
    def split_name(name: Optional[str]) -> list[str]:
        """Retorna as palavras da representação normalizada."""

        normalized = PatientNormalizer.normalize(name)
        return normalized.split() if normalized else []

    @staticmethod
    def canonical_name(name: Optional[str]) -> str:
        """Retorna a representação canônica usada internamente."""

        return " ".join(PatientNormalizer.split_name(name))

    @staticmethod
    def compare_ready(name: Optional[str]) -> str:
        """Retorna o nome canônico pronto para comparação textual."""

        return PatientNormalizer.canonical_name(name)

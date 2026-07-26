import pytest

from services.patient_normalizer import PatientNormalizer
from tests.synthetic_fixtures import (
    SYNTHETIC_PERSON_ACCENTED,
    SYNTHETIC_PERSON_ACCENTED_ASCII,
)


def test_remove_accents_can_be_used_in_isolation() -> None:
    assert PatientNormalizer.remove_accents("Pessoa Árvore Ç") == "Pessoa Arvore C"


def test_remove_special_characters_can_be_used_in_isolation() -> None:
    assert (
        PatientNormalizer.remove_special_characters("PESSOA-TESTE!")
        == "PESSOA TESTE "
    )


def test_collapse_spaces_can_be_used_in_isolation() -> None:
    assert PatientNormalizer.collapse_spaces("  PESSOA\tTESTE\nALFA  ") == (
        "PESSOA TESTE ALFA"
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("Pessoa Árvore Teste", SYNTHETIC_PERSON_ACCENTED_ASCII),
        ("pessoa teste alfa", "PESSOA TESTE ALFA"),
        ("TESTE^PESSOA^ÁRVORE", "TESTE PESSOA ARVORE"),
        ("  PESSOA    TESTE  ", "PESSOA TESTE"),
        ("PESSOA-TESTE, JR.", "PESSOA TESTE JR"),
        ("Fixture Ünica", "FIXTURE UNICA"),
        ("測試  人物", "測試 人物"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_handles_supported_input_variations(
    original: str | None,
    expected: str,
) -> None:
    assert PatientNormalizer.normalize(original) == expected


def test_split_name_returns_normalized_words() -> None:
    assert PatientNormalizer.split_name(SYNTHETIC_PERSON_ACCENTED) == [
        "PESSOA",
        "ARVORE",
        "TESTE",
    ]


def test_split_name_returns_empty_list_for_none() -> None:
    assert PatientNormalizer.split_name(None) == []


def test_canonical_name_is_always_based_on_normalize() -> None:
    original = "  Pessoa^Árvore-Teste  "

    assert PatientNormalizer.canonical_name(original) == (
        PatientNormalizer.normalize(original)
    )


def test_compare_ready_preserves_canonical_semantics() -> None:
    original = "PESSOA   D'EXEMPLO"

    assert PatientNormalizer.compare_ready(original) == "PESSOA D EXEMPLO"
    assert PatientNormalizer.compare_ready(original) == (
        PatientNormalizer.canonical_name(original)
    )


def test_very_large_name_is_processed_deterministically() -> None:
    original = "Árvore-" * 20_000

    first = PatientNormalizer.normalize(original)
    second = PatientNormalizer.normalize(original)

    assert first == second
    assert first.split() == ["ARVORE"] * 20_000


def test_normalizer_has_no_instance_state() -> None:
    assert vars(PatientNormalizer()) == {}

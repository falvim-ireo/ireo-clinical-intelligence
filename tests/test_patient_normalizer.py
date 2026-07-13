import pytest

from services.patient_normalizer import PatientNormalizer


def test_remove_accents_can_be_used_in_isolation() -> None:
    assert PatientNormalizer.remove_accents("João José Ç") == "Joao Jose C"


def test_remove_special_characters_can_be_used_in_isolation() -> None:
    assert (
        PatientNormalizer.remove_special_characters("ANA-MARIA!")
        == "ANA MARIA "
    )


def test_collapse_spaces_can_be_used_in_isolation() -> None:
    assert PatientNormalizer.collapse_spaces("  ANA\tMARIA\nSILVA  ") == (
        "ANA MARIA SILVA"
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("João da Silva", "JOAO DA SILVA"),
        ("maria souza", "MARIA SOUZA"),
        ("SILVA^JOÃO^CARLOS", "SILVA JOAO CARLOS"),
        ("  ANA    MARIA  ", "ANA MARIA"),
        ("MARIA-DA-SILVA, JR.", "MARIA DA SILVA JR"),
        ("Élodie Müller", "ELODIE MULLER"),
        ("李  小龍", "李 小龍"),
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
    assert PatientNormalizer.split_name("JOÃO DA SILVA") == [
        "JOAO",
        "DA",
        "SILVA",
    ]


def test_split_name_returns_empty_list_for_none() -> None:
    assert PatientNormalizer.split_name(None) == []


def test_canonical_name_is_always_based_on_normalize() -> None:
    original = "  João^da-Silva  "

    assert PatientNormalizer.canonical_name(original) == (
        PatientNormalizer.normalize(original)
    )


def test_compare_ready_preserves_canonical_semantics() -> None:
    original = "MARIA   D'ÁVILA"

    assert PatientNormalizer.compare_ready(original) == "MARIA D AVILA"
    assert PatientNormalizer.compare_ready(original) == (
        PatientNormalizer.canonical_name(original)
    )


def test_very_large_name_is_processed_deterministically() -> None:
    original = "João-" * 20_000

    first = PatientNormalizer.normalize(original)
    second = PatientNormalizer.normalize(original)

    assert first == second
    assert first.split() == ["JOAO"] * 20_000


def test_normalizer_has_no_instance_state() -> None:
    assert vars(PatientNormalizer()) == {}

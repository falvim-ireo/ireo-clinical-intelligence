import pytest
from acquisition.cfaz_digital_identify import CfazIdentifyError, validate_descriptor_filenames


def test_unique_descriptor_filenames_map_to_ids():
    result = validate_descriptor_filenames([
        {"stl_file_id": 1511267, "filename": "lower.zip"},
        {"stl_file_id": 1511268, "filename": "upper.zip"},
    ])
    assert result["lower.zip"] == "1511267"


@pytest.mark.parametrize("items", [
    [{"stl_file_id": 1}],
    [{"stl_file_id": 1, "filename": "A.zip"}, {"stl_file_id": 2, "filename": "a.zip"}],
])
def test_missing_or_duplicate_names_fail_closed(items):
    with pytest.raises(CfazIdentifyError):
        validate_descriptor_filenames(items)

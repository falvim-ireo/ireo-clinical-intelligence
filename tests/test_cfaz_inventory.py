import pytest
from acquisition.cfaz_inventory import CfazInventorySnapshot, RemoteState, inspect_cfaz_inventory

def test_snapshot_is_immutable_and_unknown_blocks():
    snap = inspect_cfaz_inventory(manifest_count=1, clinical_package_count=1,
        clinical_assets_count=15, exams_count=1, history_count=1, intake_count=1,
        destination_fingerprint_count=1, local_files_count=30,
        remote_state=RemoteState.UNKNOWN)
    assert snap.clinical_assets_count == 15
    assert snap.remote_state is RemoteState.UNKNOWN
    with pytest.raises(Exception):
        snap.clinical_assets_count = 0

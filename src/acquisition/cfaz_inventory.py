from dataclasses import dataclass
from enum import StrEnum
from typing import Any

class RemoteState(StrEnum):
    EXISTS = "EXISTS"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True)
class CfazInventorySnapshot:
    manifest_count: int
    clinical_package_count: int
    clinical_assets_count: int
    exams_count: int
    history_count: int
    intake_count: int
    destination_fingerprint_count: int
    local_files_count: int
    remote_state: RemoteState
    blockers: tuple[str, ...] = ()

def inspect_cfaz_inventory(*, manifest_count: int, clinical_package_count: int,
                           clinical_assets_count: int, exams_count: int,
                           history_count: int, intake_count: int,
                           destination_fingerprint_count: int,
                           local_files_count: int,
                           remote_state: RemoteState,
                           blockers: tuple[str, ...] = ()) -> CfazInventorySnapshot:
    if remote_state is RemoteState.UNKNOWN and "destino remoto não determinado" not in blockers:
        blockers = (*blockers, "destino remoto não determinado")
    return CfazInventorySnapshot(manifest_count, clinical_package_count,
        clinical_assets_count, exams_count, history_count, intake_count,
        destination_fingerprint_count, local_files_count, remote_state, tuple(blockers))

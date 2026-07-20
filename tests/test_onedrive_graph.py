import pytest
import requests

from integrations.onedrive_graph import (
    OneDriveGraphClient,
    OneDriveGraphError,
    OneDriveRootNotFoundError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses=None, error=None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def test_finds_local_root_folder_and_lists_first_items() -> None:
    session = FakeSession(
        responses=[
            FakeResponse(200, {"id": "root-item-id", "name": "Pacientes", "folder": {}}),
            FakeResponse(200, {"value": [{"id": "1", "name": "Paciente A"}]}),
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)

    folder = client.find_root_folder("Clínica/Pacientes")
    items = client.list_first_items(folder.item_id)

    assert folder.item_id == "root-item-id"
    assert folder.name == "Pacientes"
    assert folder.is_remote is False
    assert folder.remote_item_id is None
    assert folder.remote_drive_id is None
    assert [item["name"] for item in items] == ["Paciente A"]
    assert "Cl%C3%ADnica/Pacientes" in session.calls[0][0]
    assert "/me/drive/items/root-item-id/children" in session.calls[1][0]
    assert session.calls[1][1]["params"]["$top"] == "10"
    assert session.calls[0][1]["timeout"] == (10.0, 30.0)


def test_finds_remote_root_folder_and_lists_remote_items() -> None:
    session = FakeSession(
        responses=[
            FakeResponse(
                200,
                {
                    "id": "shortcut-id",
                    "name": "Atalho",
                    "remoteItem": {
                        "id": "remote-item-id",
                        "name": "Pasta pacientes 2026",
                        "folder": {},
                        "parentReference": {"driveId": "remote-drive-id"},
                    },
                },
            ),
            FakeResponse(200, {"value": [{"id": "1", "name": "Paciente A"}]}),
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)

    folder = client.find_root_folder("Pasta pacientes 2026")
    items = client.list_first_items(folder.item_id)

    assert folder.item_id == "shortcut-id"
    assert folder.name == "Pasta pacientes 2026"
    assert folder.is_remote is True
    assert folder.remote_item_id == "remote-item-id"
    assert folder.remote_drive_id == "remote-drive-id"
    assert [item["name"] for item in items] == ["Paciente A"]
    assert (
        "/drives/remote-drive-id/items/remote-item-id/children"
        in session.calls[1][0]
    )
    assert "shortcut-id" not in session.calls[1][0]


def test_rejects_remote_item_that_is_a_file() -> None:
    client = OneDriveGraphClient(
        "fixture-token",
        session=FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "id": "shortcut-id",
                        "name": "Arquivo remoto",
                        "remoteItem": {
                            "id": "remote-file-id",
                            "name": "Arquivo remoto",
                            "file": {},
                        },
                    },
                )
            ]
        ),
    )

    with pytest.raises(OneDriveRootNotFoundError, match="não corresponde a uma pasta"):
        client.find_root_folder("Arquivo remoto")


def test_rejects_item_without_local_or_remote_folder() -> None:
    client = OneDriveGraphClient(
        "fixture-token",
        session=FakeSession(
            [FakeResponse(200, {"id": "item-id", "name": "Item desconhecido"})]
        ),
    )

    with pytest.raises(OneDriveRootNotFoundError, match="não corresponde a uma pasta"):
        client.find_root_folder("Item desconhecido")


def test_root_folder_not_found() -> None:
    client = OneDriveGraphClient(
        "fixture-token", session=FakeSession([FakeResponse(404)])
    )

    with pytest.raises(OneDriveRootNotFoundError, match="não foi encontrada"):
        client.find_root_folder("Clínica/Pacientes")


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "401"),
        (403, "403"),
        (429, "429"),
    ],
)
def test_sanitizes_expected_graph_errors(status_code: int, message: str) -> None:
    client = OneDriveGraphClient(
        "fixture-token", session=FakeSession([FakeResponse(status_code)])
    )

    with pytest.raises(OneDriveGraphError, match=message):
        client.get_authenticated_user()


def test_graph_timeout_is_sanitized() -> None:
    client = OneDriveGraphClient(
        "fixture-token", session=FakeSession(error=requests.Timeout("secret URL"))
    )

    with pytest.raises(OneDriveGraphError, match="Tempo limite") as captured:
        client.get_drive()

    assert "secret" not in str(captured.value)

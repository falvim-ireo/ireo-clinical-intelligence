import pytest
import requests

from integrations.onedrive_graph import (
    GraphFolder,
    GraphUploadedItem,
    OneDriveGraphClient,
    OneDriveGraphError,
    OneDriveFolderConflictError,
    OneDriveRootNotFoundError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None) -> None:
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

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

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    def patch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def test_finds_local_root_folder_and_lists_first_items() -> None:
    session = FakeSession(
        responses=[
            FakeResponse(
                200,
                {
                    "id": "root-item-id",
                    "name": "Pacientes",
                    "folder": {},
                    "parentReference": {"driveId": "local-drive-id"},
                },
            ),
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
    assert folder.drive_id == "local-drive-id"
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


def test_renames_existing_remote_file_without_automatic_conflict_suffix() -> None:
    session = FakeSession([
        FakeResponse(200, {"value": [{
            "id": "file-id", "name": "sem-extensao", "file": {},
        }]}),
        FakeResponse(200, {"id": "file-id", "name": "imagem_001.jpg"}),
    ])
    client = OneDriveGraphClient("fixture-token", session=session)

    client.rename_child_file(
        GraphFolder("folder-id", "Exame", drive_id="drive-id"),
        "sem-extensao",
        "imagem_001.jpg",
    )

    assert session.calls[1][1]["json"] == {"name": "imagem_001.jpg"}
    assert session.calls[1][0].endswith("/drives/drive-id/items/file-id")


def test_moves_file_to_clinical_folder_with_deterministic_name() -> None:
    session = FakeSession([
        FakeResponse(200, {"value": []}),
        FakeResponse(200, {"value": [{
            "id": "file-id", "name": "sem-extensao", "file": {},
        }]}),
        FakeResponse(200, {"id": "file-id", "name": "radiografia_001.jpg"}),
    ])
    client = OneDriveGraphClient("fixture-token", session=session)

    client.move_child_file(
        GraphFolder("exam-id", "Exame", drive_id="drive-id"),
        "sem-extensao",
        GraphFolder("radiographs-id", "01 - Radiografias", drive_id="drive-id"),
        "radiografia_001.jpg",
    )

    assert session.calls[2][1]["json"] == {
        "name": "radiografia_001.jpg",
        "parentReference": {"id": "radiographs-id"},
    }


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


def test_uploads_small_file_to_local_folder(tmp_path) -> None:
    local_file = tmp_path / "upload.txt"
    content = b"conteudo de teste"
    local_file.write_bytes(content)
    session = FakeSession(
        [FakeResponse(201, {"id": "uploaded-id", "name": "upload.txt", "size": 17})]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    folder = GraphFolder(
        item_id="local-folder-id",
        name="Pacientes",
        drive_id="local-drive-id",
    )

    uploaded = client.upload_small_file(folder, local_file)

    assert uploaded.name == "upload.txt"
    assert uploaded.size == len(content)
    assert uploaded.has_id is True
    assert (
        "/drives/local-drive-id/items/local-folder-id:/upload.txt:/content"
        in session.calls[0][0]
    )
    assert session.calls[0][1]["data"] == content
    assert (
        session.calls[0][1]["headers"]["Content-Type"]
        == "application/octet-stream"
    )


def test_uploads_jpeg_with_detected_content_type(tmp_path) -> None:
    local_file = tmp_path / "imagem_001.jpg"
    content = b"\xff\xd8\xff\xe0" + b"jpeg"
    local_file.write_bytes(content)
    session = FakeSession([
        FakeResponse(201, {"id": "jpeg-id", "name": local_file.name, "size": len(content)})
    ])
    client = OneDriveGraphClient("fixture-token", session=session)

    client.upload_small_file(
        GraphFolder("folder-id", "Exame", drive_id="drive-id"), local_file
    )

    assert session.calls[0][1]["headers"]["Content-Type"] == "image/jpeg"


def test_large_file_uses_upload_session_and_commits_final_item(tmp_path) -> None:
    chunk_size = 320 * 1024
    content = b"a" * (chunk_size + 17)
    local_file = tmp_path / "large.bin"
    local_file.write_bytes(content)
    upload_url = "https://upload.example/session?opaque=secret"
    session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": upload_url}),
            FakeResponse(202, {"nextExpectedRanges": [f"{chunk_size}-"]}),
            FakeResponse(
                201,
                {"id": "large-id", "name": "large.bin", "size": len(content)},
            ),
        ]
    )
    client = OneDriveGraphClient(
        "fixture-token", session=session, large_upload_chunk_size=chunk_size
    )
    client.MAX_SMALL_UPLOAD_BYTES = 1
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")
    progress: list[tuple[int, int]] = []

    uploaded = client.upload_small_file(
        folder,
        local_file,
        progress_callback=lambda sent, total: progress.append((sent, total)),
    )

    assert uploaded == GraphUploadedItem("large.bin", len(content), True)
    assert "createUploadSession" in session.calls[0][0]
    assert session.calls[1][0] == upload_url
    assert session.calls[1][1]["headers"]["Content-Range"] == (
        f"bytes 0-{chunk_size - 1}/{len(content)}"
    )
    assert session.calls[2][1]["headers"]["Content-Range"] == (
        f"bytes {chunk_size}-{len(content) - 1}/{len(content)}"
    )
    assert "Authorization" not in session.calls[1][1]["headers"]
    assert progress == [(chunk_size, len(content)), (len(content), len(content))]


def test_large_upload_propagates_error_during_chunk(tmp_path) -> None:
    local_file = tmp_path / "large.bin"
    local_file.write_bytes(b"a" * (320 * 1024))
    upload_url = "https://upload.example/session?opaque=secret"
    session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": upload_url}),
            FakeResponse(
                400,
                {"error": {"code": "invalidRange", "message": "Chunk inválido."}},
                headers={"request-id": "chunk-request-id"},
            ),
        ]
    )
    client = OneDriveGraphClient(
        "fixture-token", session=session, large_upload_chunk_size=320 * 1024
    )
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")

    with pytest.raises(OneDriveGraphError, match="Chunk inválido") as captured:
        client.upload_large_file(folder, local_file)

    assert captured.value.graph_code == "invalidRange"
    assert captured.value.request_id == "chunk-request-id"
    assert captured.value.endpoint == "https://upload.example/session"
    assert "opaque=secret" not in str(captured.value)


def test_large_upload_resumes_from_server_offset_after_transient_failure(
    tmp_path,
) -> None:
    chunk_size = 320 * 1024
    content = b"a" * (chunk_size + 11)
    local_file = tmp_path / "large.bin"
    local_file.write_bytes(content)
    upload_url = "https://upload.example/session?opaque=secret"
    session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": upload_url}),
            FakeResponse(503, {"error": {"code": "serviceUnavailable"}}),
            FakeResponse(200, {"nextExpectedRanges": [f"{chunk_size}-"]}),
            FakeResponse(
                201,
                {"id": "large-id", "name": "large.bin", "size": len(content)},
            ),
        ]
    )
    client = OneDriveGraphClient(
        "fixture-token", session=session, large_upload_chunk_size=chunk_size
    )
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")
    retries: list[tuple[int, int, int]] = []

    uploaded = client.upload_large_file(
        folder,
        local_file,
        retry_callback=lambda offset, attempt, maximum: retries.append(
            (offset, attempt, maximum)
        ),
    )

    assert uploaded.size == len(content)
    assert session.calls[2][0] == upload_url
    assert session.calls[3][1]["headers"]["Content-Range"] == (
        f"bytes {chunk_size}-{len(content) - 1}/{len(content)}"
    )
    assert retries == [(chunk_size, 2, 4)]


def test_uploads_small_file_to_remote_folder(tmp_path) -> None:
    local_file = tmp_path / "upload.txt"
    local_file.write_text("remoto", encoding="utf-8")
    session = FakeSession(
        [FakeResponse(200, {"id": "uploaded-id", "name": "upload.txt", "size": 6})]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    folder = GraphFolder(
        item_id="shortcut-id",
        name="Pasta compartilhada",
        is_remote=True,
        remote_item_id="remote-folder-id",
        remote_drive_id="remote-drive-id",
    )

    uploaded = client.upload_small_file(folder, local_file)

    assert uploaded.name == "upload.txt"
    assert uploaded.size == 6
    assert (
        "/drives/remote-drive-id/items/remote-folder-id:/upload.txt:/content"
        in session.calls[0][0]
    )
    assert "shortcut-id" not in session.calls[0][0]


def test_upload_rejects_missing_local_file(tmp_path) -> None:
    client = OneDriveGraphClient("fixture-token", session=FakeSession())
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")

    with pytest.raises(OneDriveGraphError, match="não encontrado"):
        client.upload_small_file(folder, tmp_path / "inexistente.txt")


def test_upload_reports_http_error_without_response_details(tmp_path) -> None:
    local_file = tmp_path / "upload.txt"
    local_file.write_text("teste", encoding="utf-8")
    client = OneDriveGraphClient(
        "fixture-token", session=FakeSession([FakeResponse(403, {"secret": "value"})])
    )
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")

    with pytest.raises(OneDriveGraphError, match="403") as captured:
        client.upload_small_file(folder, local_file)

    assert "secret" not in str(captured.value)


def test_upload_uses_custom_remote_filename(tmp_path) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("teste", encoding="utf-8")
    session = FakeSession(
        [FakeResponse(201, {"name": "nome remoto.txt", "size": 5})]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    folder = GraphFolder("folder-id", "Pacientes", drive_id="drive-id")

    uploaded = client.upload_small_file(
        folder, local_file, remote_filename="nome remoto.txt"
    )

    assert uploaded.name == "nome remoto.txt"
    assert uploaded.has_id is False
    assert "nome%20remoto.txt" in session.calls[0][0]


@pytest.mark.parametrize(
    "folder",
    [
        GraphFolder("folder-id", "Sem drive"),
        GraphFolder("", "Sem item", drive_id="drive-id"),
        GraphFolder(
            "shortcut-id",
            "Remota sem drive",
            is_remote=True,
            remote_item_id="remote-item-id",
        ),
        GraphFolder(
            "shortcut-id",
            "Remota sem item",
            is_remote=True,
            remote_drive_id="remote-drive-id",
        ),
    ],
)
def test_upload_rejects_folder_without_drive_id_or_item_id(tmp_path, folder) -> None:
    local_file = tmp_path / "upload.txt"
    local_file.write_text("teste", encoding="utf-8")
    client = OneDriveGraphClient("fixture-token", session=FakeSession())

    with pytest.raises(OneDriveGraphError, match="driveId e itemId"):
        client.upload_small_file(folder, local_file)


def test_finds_existing_child_folder() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "child-id",
                            "name": "Teste Paciente API",
                            "folder": {},
                            "parentReference": {"driveId": "drive-id"},
                        }
                    ]
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    child = client.find_child_folder(parent, "Teste Paciente API")

    assert child == GraphFolder(
        "child-id", "Teste Paciente API", drive_id="drive-id"
    )
    assert "/drives/drive-id/items/parent-id/children" in session.calls[0][0]


def test_find_child_folder_ignores_file_with_same_name() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "file-id",
                            "name": "Teste Paciente API",
                            "file": {},
                        }
                    ]
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    assert client.find_child_folder(parent, "Teste Paciente API") is None


def test_creates_child_folder() -> None:
    session = FakeSession(
        [
            FakeResponse(
                201,
                {
                    "id": "created-id",
                    "name": "Teste Paciente API",
                    "folder": {},
                    "parentReference": {"driveId": "drive-id"},
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    created = client.create_folder(parent, "Teste Paciente API")

    assert created == GraphFolder(
        "created-id", "Teste Paciente API", drive_id="drive-id"
    )
    assert session.calls[0][1]["json"] == {
        "name": "Teste Paciente API",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    assert session.calls[0][1]["headers"]["Content-Type"] == "application/json"


def test_ensure_folder_returns_existing_folder_without_creating() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {"id": "existing-id", "name": "Paciente", "folder": {}}
                    ]
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    folder = client.ensure_folder(parent, "Paciente")

    assert folder.item_id == "existing-id"
    assert len(session.calls) == 1


def test_ensure_folder_creates_missing_folder() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"value": []}),
            FakeResponse(
                201,
                {"id": "created-id", "name": "Paciente", "folder": {}},
            ),
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    folder = client.ensure_folder(parent, "Paciente")

    assert folder == GraphFolder("created-id", "Paciente", drive_id="drive-id")
    assert len(session.calls) == 2


def test_ensure_folder_path_creates_nested_path() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"value": []}),
            FakeResponse(
                201,
                {"id": "patient-id", "name": "Paciente", "folder": {}},
            ),
            FakeResponse(200, {"value": []}),
            FakeResponse(
                201,
                {"id": "exam-id", "name": "2026-07-21", "folder": {}},
            ),
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    root = GraphFolder("root-id", "Raiz", drive_id="drive-id")

    final_folder = client.ensure_folder_path(
        root, ["Paciente", "2026-07-21"]
    )

    assert final_folder == GraphFolder("exam-id", "2026-07-21", drive_id="drive-id")
    assert "/items/root-id/children" in session.calls[0][0]
    assert "/items/patient-id/children" in session.calls[2][0]


def test_lists_and_finds_folder_under_remote_parent() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "shortcut-child-id",
                            "name": "Atalho",
                            "remoteItem": {
                                "id": "remote-child-id",
                                "name": "Paciente remoto",
                                "folder": {},
                                "parentReference": {
                                    "driveId": "child-remote-drive-id"
                                },
                            },
                        }
                    ]
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder(
        "shortcut-parent-id",
        "Raiz remota",
        is_remote=True,
        remote_item_id="remote-parent-id",
        remote_drive_id="remote-parent-drive-id",
    )

    child = client.find_child_folder(parent, "Paciente remoto")

    assert child is not None
    assert child.is_remote is True
    assert child.item_id == "shortcut-child-id"
    assert child.remote_item_id == "remote-child-id"
    assert child.remote_drive_id == "child-remote-drive-id"
    assert (
        "/drives/remote-parent-drive-id/items/remote-parent-id/children"
        in session.calls[0][0]
    )


def test_creates_folder_under_remote_parent_using_real_ids() -> None:
    session = FakeSession(
        [
            FakeResponse(
                201,
                {
                    "id": "created-id",
                    "name": "Paciente",
                    "folder": {},
                    "parentReference": {"driveId": "remote-drive-id"},
                },
            )
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder(
        "shortcut-id",
        "Raiz remota",
        is_remote=True,
        remote_item_id="remote-parent-id",
        remote_drive_id="remote-drive-id",
    )

    created = client.create_folder(parent, "Paciente")

    assert created == GraphFolder("created-id", "Paciente", drive_id="remote-drive-id")
    assert (
        "/drives/remote-drive-id/items/remote-parent-id/children"
        in session.calls[0][0]
    )
    assert "shortcut-id" not in session.calls[0][0]


def test_create_folder_reports_name_conflict() -> None:
    client = OneDriveGraphClient(
        "fixture-token", session=FakeSession([FakeResponse(409)])
    )
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    with pytest.raises(OneDriveFolderConflictError, match="Já existe"):
        client.create_folder(parent, "Paciente")


def test_ensure_folder_recovers_from_concurrent_name_conflict() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"value": []}),
            FakeResponse(409),
            FakeResponse(
                200,
                {"value": [{"id": "existing-id", "name": "Paciente", "folder": {}}]},
            ),
        ]
    )
    client = OneDriveGraphClient("fixture-token", session=session)
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    folder = client.ensure_folder(parent, "Paciente")

    assert folder == GraphFolder("existing-id", "Paciente", drive_id="drive-id")
    assert len(session.calls) == 3


@pytest.mark.parametrize("folder_name", ["", "   "])
def test_create_folder_rejects_empty_name(folder_name: str) -> None:
    client = OneDriveGraphClient("fixture-token", session=FakeSession())
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    with pytest.raises(OneDriveGraphError, match="Nome da pasta"):
        client.create_folder(parent, folder_name)


@pytest.mark.parametrize(
    "parent",
    [
        GraphFolder("parent-id", "Sem drive"),
        GraphFolder("", "Sem item", drive_id="drive-id"),
        GraphFolder(
            "shortcut-id",
            "Remota sem drive",
            is_remote=True,
            remote_item_id="remote-id",
        ),
        GraphFolder(
            "shortcut-id",
            "Remota sem item",
            is_remote=True,
            remote_drive_id="remote-drive-id",
        ),
    ],
)
def test_create_folder_rejects_missing_drive_id_or_item_id(parent) -> None:
    client = OneDriveGraphClient("fixture-token", session=FakeSession())

    with pytest.raises(OneDriveGraphError, match="driveId e itemId"):
        client.create_folder(parent, "Paciente")


def test_create_folder_reports_generic_http_error() -> None:
    client = OneDriveGraphClient(
        "fixture-token",
        session=FakeSession([FakeResponse(500, {"secret": "details"})]),
    )
    parent = GraphFolder("parent-id", "Raiz", drive_id="drive-id")

    with pytest.raises(OneDriveGraphError, match="500") as captured:
        client.create_folder(parent, "Paciente")

    assert "secret" not in str(captured.value)


def test_graph_error_preserves_complete_response_diagnostics() -> None:
    response = FakeResponse(
        403,
        {
            "error": {
                "code": "accessDenied",
                "message": "A operação completa foi recusada pelo administrador.",
                "innerError": {
                    "request-id": "inner-request-id",
                    "client-request-id": "inner-client-request-id",
                },
            }
        },
        headers={
            "request-id": "header-request-id",
            "client-request-id": "header-client-request-id",
        },
    )
    session = FakeSession([response])
    client = OneDriveGraphClient("fixture-token", session=session)

    with pytest.raises(OneDriveGraphError) as captured:
        client.get_authenticated_user()

    error = captured.value
    assert error.http_status == 403
    assert error.graph_code == "accessDenied"
    assert error.graph_message == "A operação completa foi recusada pelo administrador."
    assert error.request_id == "header-request-id"
    assert error.client_request_id == "header-client-request-id"
    assert error.endpoint == "https://graph.microsoft.com/v1.0/me"
    diagnostic = str(error)
    assert "HTTP status=403" in diagnostic
    assert "Graph code=accessDenied" in diagnostic
    assert "Graph message=A operação completa foi recusada pelo administrador." in diagnostic
    assert "request-id=header-request-id" in diagnostic
    assert "client-request-id=header-client-request-id" in diagnostic
    assert "endpoint=https://graph.microsoft.com/v1.0/me" in diagnostic

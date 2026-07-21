"""Cliente mínimo para consultas e uploads no OneDrive via Microsoft Graph."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


class OneDriveGraphError(RuntimeError):
    """Falha sanitizada ao consultar o OneDrive pelo Microsoft Graph."""


class OneDriveRootNotFoundError(OneDriveGraphError):
    """A pasta clínica configurada não foi localizada."""


@dataclass(frozen=True)
class GraphUser:
    display_name: str | None
    email: str | None


@dataclass(frozen=True)
class GraphDrive:
    drive_id: str


@dataclass(frozen=True)
class GraphFolder:
    item_id: str
    name: str
    is_remote: bool = False
    remote_item_id: str | None = None
    remote_drive_id: str | None = None
    drive_id: str | None = None


@dataclass(frozen=True)
class GraphUploadedItem:
    name: str
    size: int
    has_id: bool


class OneDriveGraphClient:
    """Executa consultas e uploads pequenos no drive do usuário autenticado."""

    BASE_URL = "https://graph.microsoft.com/v1.0"
    MAX_SMALL_UPLOAD_BYTES = 250 * 1024 * 1024

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
    ) -> None:
        if not access_token:
            raise OneDriveGraphError("Token de acesso do Microsoft Graph ausente.")
        self._access_token = access_token
        self._session = session or requests.Session()
        self._timeout = timeout
        self._remote_folders: dict[str, tuple[str | None, str | None]] = {}

    def get_authenticated_user(self) -> GraphUser:
        payload = self._get("/me", params={"$select": "displayName,mail,userPrincipalName"})
        return GraphUser(
            display_name=self._optional_text(payload.get("displayName")),
            email=(
                self._optional_text(payload.get("mail"))
                or self._optional_text(payload.get("userPrincipalName"))
            ),
        )

    def get_drive(self) -> GraphDrive:
        payload = self._get("/me/drive", params={"$select": "id"})
        drive_id = self._required_text(payload, "id", "Drive ID")
        return GraphDrive(drive_id=drive_id)

    def find_root_folder(self, configured_root: str) -> GraphFolder:
        normalized = configured_root.strip().strip("/\\")
        if not normalized:
            raise OneDriveGraphError("MS_GRAPH_ONEDRIVE_ROOT não foi configurado.")
        encoded_path = "/".join(quote(part, safe="") for part in normalized.replace("\\", "/").split("/") if part)
        try:
            payload = self._get(
                f"/me/drive/root:/{encoded_path}",
                params={"$select": "id,name,folder,parentReference,remoteItem"},
            )
        except OneDriveRootNotFoundError:
            raise
        item_id = self._required_text(payload, "id", "Root Item ID")
        if isinstance(payload.get("folder"), dict):
            name = self._required_text(payload, "name", "nome da pasta")
            parent_reference = payload.get("parentReference")
            drive_id = (
                self._optional_text(parent_reference.get("driveId"))
                if isinstance(parent_reference, dict)
                else None
            )
            return GraphFolder(item_id=item_id, name=name, drive_id=drive_id)

        remote_item = payload.get("remoteItem")
        if isinstance(remote_item, dict) and isinstance(
            remote_item.get("folder"), dict
        ):
            parent_reference = remote_item.get("parentReference")
            remote_item_id = self._optional_text(remote_item.get("id"))
            remote_drive_id = (
                self._optional_text(parent_reference.get("driveId"))
                if isinstance(parent_reference, dict)
                else None
            )
            name = (
                self._optional_text(remote_item.get("name"))
                or self._required_text(payload, "name", "nome da pasta")
            )
            self._remote_folders[item_id] = (remote_drive_id, remote_item_id)
            return GraphFolder(
                item_id=item_id,
                name=name,
                is_remote=True,
                remote_item_id=remote_item_id,
                remote_drive_id=remote_drive_id,
            )

        raise OneDriveRootNotFoundError(
            "O caminho configurado no OneDrive não corresponde a uma pasta."
        )

    def list_first_items(self, folder_item_id: str, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 100)
        encoded_id = quote(folder_item_id, safe="")
        if folder_item_id in self._remote_folders:
            remote_drive_id, remote_item_id = self._remote_folders[folder_item_id]
            if remote_drive_id is None or remote_item_id is None:
                raise OneDriveGraphError(
                    "A pasta remota não informou driveId e itemId para listar seus itens."
                )
            encoded_drive_id = quote(remote_drive_id, safe="")
            encoded_remote_item_id = quote(remote_item_id, safe="")
            children_path = (
                f"/drives/{encoded_drive_id}/items/{encoded_remote_item_id}/children"
            )
        else:
            children_path = f"/me/drive/items/{encoded_id}/children"
        payload = self._get(
            children_path,
            params={"$select": "id,name,file,folder", "$top": str(safe_limit)},
        )
        values = payload.get("value")
        if not isinstance(values, list):
            raise OneDriveGraphError("Resposta inválida ao listar itens do OneDrive.")
        return [item for item in values if isinstance(item, dict)]

    def upload_small_file(
        self,
        parent_folder: GraphFolder,
        local_file_path: str | Path,
        remote_filename: str | None = None,
    ) -> GraphUploadedItem:
        local_path = Path(local_file_path)
        if not local_path.is_file():
            raise OneDriveGraphError("Arquivo local para upload não encontrado.")
        try:
            content = local_path.read_bytes()
        except OSError:
            raise OneDriveGraphError(
                "Não foi possível ler o arquivo local para upload."
            ) from None
        file_size = len(content)
        if file_size > self.MAX_SMALL_UPLOAD_BYTES:
            raise OneDriveGraphError(
                "Arquivo excede o limite permitido para upload direto."
            )

        filename = (remote_filename or local_path.name).strip()
        if not filename:
            raise OneDriveGraphError("Nome remoto do arquivo não foi informado.")
        if "/" in filename or "\\" in filename:
            raise OneDriveGraphError("Nome remoto do arquivo é inválido.")

        if parent_folder.is_remote:
            drive_id = parent_folder.remote_drive_id
            item_id = parent_folder.remote_item_id
        else:
            drive_id = parent_folder.drive_id
            item_id = parent_folder.item_id
        if not drive_id or not item_id:
            raise OneDriveGraphError(
                "A pasta de destino não informou driveId e itemId para o upload."
            )

        encoded_drive_id = quote(drive_id, safe="")
        encoded_item_id = quote(item_id, safe="")
        encoded_filename = quote(filename, safe="")
        path = (
            f"/drives/{encoded_drive_id}/items/{encoded_item_id}:"
            f"/{encoded_filename}:/content"
        )
        try:
            response = self._session.put(
                f"{self.BASE_URL}{path}",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/octet-stream",
                },
                data=content,
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError(
                "Tempo limite excedido ao enviar arquivo ao Microsoft Graph."
            ) from None
        except (OSError, requests.RequestException):
            raise OneDriveGraphError(
                "Falha ao enviar arquivo ao Microsoft Graph."
            ) from None

        if not 200 <= response.status_code < 300:
            raise OneDriveGraphError(
                f"Microsoft Graph retornou um erro HTTP ({response.status_code}) "
                "durante o upload."
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError(
                "Resposta inválida do Microsoft Graph após o upload."
            ) from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError(
                "Resposta inválida do Microsoft Graph após o upload."
            )
        uploaded_name = self._required_text(payload, "name", "nome do arquivo")
        uploaded_size = payload.get("size")
        if not isinstance(uploaded_size, int) or uploaded_size < 0:
            raise OneDriveGraphError(
                "Tamanho do arquivo ausente na resposta do Microsoft Graph."
            )
        return GraphUploadedItem(
            name=uploaded_name,
            size=uploaded_size,
            has_id=self._optional_text(payload.get("id")) is not None,
        )

    def _get(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._session.get(
                f"{self.BASE_URL}{path}",
                headers={"Authorization": f"Bearer {self._access_token}"},
                params=params,
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError(
                "Tempo limite excedido ao consultar o Microsoft Graph."
            ) from None
        except requests.RequestException:
            raise OneDriveGraphError(
                "Falha de comunicação ao consultar o Microsoft Graph."
            ) from None

        if response.status_code == 404:
            raise OneDriveRootNotFoundError(
                "A pasta raiz configurada não foi encontrada no OneDrive."
            )
        messages = {
            401: "Autenticação rejeitada pelo Microsoft Graph (401).",
            403: "Acesso ao OneDrive negado pelo Microsoft Graph (403).",
            429: "Limite de requisições do Microsoft Graph atingido (429).",
        }
        if response.status_code in messages:
            raise OneDriveGraphError(messages[response.status_code])
        if not 200 <= response.status_code < 300:
            raise OneDriveGraphError(
                f"Microsoft Graph retornou um erro HTTP ({response.status_code})."
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError("Resposta inválida do Microsoft Graph.") from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError("Resposta inválida do Microsoft Graph.")
        return payload

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _required_text(cls, payload: dict[str, Any], field: str, label: str) -> str:
        value = cls._optional_text(payload.get(field))
        if value is None:
            raise OneDriveGraphError(f"{label} ausente na resposta do Microsoft Graph.")
        return value

"""Cliente mínimo para consultas e uploads no OneDrive via Microsoft Graph."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote, urlsplit

import requests


class OneDriveGraphError(RuntimeError):
    """Falha do Graph com os metadados de diagnóstico retornados pela API."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        graph_code: str | None = None,
        graph_message: str | None = None,
        request_id: str | None = None,
        client_request_id: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.graph_code = graph_code
        self.graph_message = graph_message
        self.request_id = request_id
        self.client_request_id = client_request_id
        self.endpoint = endpoint
        details = []
        if http_status is not None or endpoint is not None:
            unavailable = "<não retornado>"
            details = [
                f"HTTP status={http_status if http_status is not None else unavailable}",
                f"Graph code={graph_code or unavailable}",
                f"Graph message={graph_message or message}",
                f"request-id={request_id or unavailable}",
                f"client-request-id={client_request_id or unavailable}",
                f"endpoint={endpoint or unavailable}",
            ]
        super().__init__(f"{message} ({'; '.join(details)})" if details else message)


class OneDriveRootNotFoundError(OneDriveGraphError):
    """A pasta clínica configurada não foi localizada."""


class OneDriveFolderConflictError(OneDriveGraphError):
    """Já existe um item com o nome solicitado na pasta de destino."""


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


@dataclass(frozen=True)
class GraphCreatedItemReference:
    drive_id: str
    item_id: str
    etag: str
    created_by_transaction: bool = True


@dataclass(frozen=True)
class GraphRollbackVerification:
    verified_absent: bool
    delete_status: int | None
    verification_attempts: int
    delete_transport_uncertain: bool = False


class GraphRollbackJournal:
    """Journal efêmero restrito aos itens criados pela transação atual."""

    def __init__(self) -> None:
        self._references: list[GraphCreatedItemReference] = []

    def record(self, reference: GraphCreatedItemReference) -> None:
        if (
            not isinstance(reference, GraphCreatedItemReference)
            or not reference.created_by_transaction
        ):
            raise OneDriveGraphError(
                "Somente itens criados pela transação podem entrar no journal."
            )
        self._references.append(reference)

    def rollback_reference(
        self,
        client: OneDriveGraphClient,
        reference: GraphCreatedItemReference,
    ) -> GraphRollbackVerification:
        if reference not in self._references:
            raise OneDriveGraphError(
                "Item ausente do journal transacional; exclusão recusada."
            )
        return client.delete_created_item_verified(reference)

    def rollback_all(
        self, client: OneDriveGraphClient
    ) -> tuple[list[GraphRollbackVerification], int]:
        results: list[GraphRollbackVerification] = []
        failures = 0
        for reference in reversed(self._references):
            try:
                results.append(client.delete_created_item_verified(reference))
            except OneDriveGraphError:
                failures += 1
        return results, failures


class OneDriveGraphClient:
    """Executa consultas e uploads pequenos no drive do usuário autenticado."""

    BASE_URL = "https://graph.microsoft.com/v1.0"
    MAX_SMALL_UPLOAD_BYTES = 250 * 1024 * 1024
    DEFAULT_LARGE_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024
    UPLOAD_CHUNK_ALIGNMENT = 320 * 1024
    VERIFIED_COMPENSATION_CAPABILITY = "graph-item-id-etag-delete-v1"

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        large_upload_chunk_size: int = DEFAULT_LARGE_UPLOAD_CHUNK_SIZE,
        large_upload_max_retries: int = 3,
    ) -> None:
        if not access_token:
            raise OneDriveGraphError("Token de acesso do Microsoft Graph ausente.")
        self._access_token = access_token
        self._session = session or requests.Session()
        self._timeout = timeout
        if (
            large_upload_chunk_size <= 0
            or large_upload_chunk_size % self.UPLOAD_CHUNK_ALIGNMENT != 0
        ):
            raise ValueError(
                "O tamanho do bloco deve ser um múltiplo positivo de 320 KiB."
            )
        if large_upload_max_retries < 0:
            raise ValueError("O número máximo de retomadas não pode ser negativo.")
        self.large_upload_chunk_size = large_upload_chunk_size
        self.large_upload_max_retries = large_upload_max_retries
        self._remote_folders: dict[str, tuple[str | None, str | None]] = {}

    def read_only(self) -> "OneDriveGraphReadOnly":
        """Expõe somente as consultas necessárias ao preflight remoto."""
        return OneDriveGraphReadOnly(self)

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

    def list_children(self, folder: GraphFolder) -> list[dict[str, Any]]:
        drive_id, item_id = self._folder_location(folder)
        encoded_drive_id = quote(drive_id, safe="")
        encoded_item_id = quote(item_id, safe="")
        payload = self._get(
            f"/drives/{encoded_drive_id}/items/{encoded_item_id}/children",
            params={
                "$select": "id,name,size,file,folder,parentReference,remoteItem",
                "$top": "999",
            },
        )
        values = payload.get("value")
        if not isinstance(values, list):
            raise OneDriveGraphError("Resposta inválida ao listar itens do OneDrive.")
        return [item for item in values if isinstance(item, dict)]

    def find_child_folder(
        self, parent_folder: GraphFolder, folder_name: str
    ) -> GraphFolder | None:
        expected_name = folder_name.strip()
        if not expected_name:
            raise OneDriveGraphError("Nome da pasta não foi informado.")
        for item in self.list_children(parent_folder):
            item_name = self._optional_text(item.get("name"))
            if item_name is None:
                continue
            item_id = self._optional_text(item.get("id"))
            if (
                item_name.casefold() == expected_name.casefold()
                and isinstance(item.get("folder"), dict)
                and item_id is not None
            ):
                return self.folder_from_child_item(parent_folder, item)

            remote_item = item.get("remoteItem")
            if not isinstance(remote_item, dict) or not isinstance(
                remote_item.get("folder"), dict
            ):
                continue
            remote_name = self._optional_text(remote_item.get("name")) or item_name
            if remote_name.casefold() != expected_name.casefold():
                continue
            return self.folder_from_child_item(parent_folder, item)
        return None

    def folder_from_child_item(
        self, parent_folder: GraphFolder, item: dict[str, Any]
    ) -> GraphFolder:
        """Converte um item já enumerado sem repetir a chamada ``children``."""
        item_id = self._required_text(item, "id", "Item ID")
        name = self._required_text(item, "name", "nome da pasta")
        remote_item = item.get("remoteItem")
        if isinstance(remote_item, dict) and isinstance(remote_item.get("folder"), dict):
            remote_parent = remote_item.get("parentReference")
            return GraphFolder(
                item_id=item_id,
                name=self._optional_text(remote_item.get("name")) or name,
                is_remote=True,
                remote_item_id=self._optional_text(remote_item.get("id")),
                remote_drive_id=(
                    self._optional_text(remote_parent.get("driveId"))
                    if isinstance(remote_parent, dict) else None
                ),
            )
        if not isinstance(item.get("folder"), dict):
            raise OneDriveGraphError("O item enumerado não corresponde a uma pasta.")
        parent_reference = item.get("parentReference")
        parent_drive_id, _ = self._folder_location(parent_folder)
        drive_id = (
            self._optional_text(parent_reference.get("driveId"))
            if isinstance(parent_reference, dict) else None
        )
        return GraphFolder(item_id=item_id, name=name, drive_id=drive_id or parent_drive_id)

    def create_folder(
        self, parent_folder: GraphFolder, folder_name: str
    ) -> GraphFolder:
        name = folder_name.strip()
        if not name:
            raise OneDriveGraphError("Nome da pasta não foi informado.")
        if "/" in name or "\\" in name:
            raise OneDriveGraphError("Nome da pasta é inválido.")
        drive_id, item_id = self._folder_location(parent_folder)
        encoded_drive_id = quote(drive_id, safe="")
        encoded_item_id = quote(item_id, safe="")
        payload = self._post(
            f"/drives/{encoded_drive_id}/items/{encoded_item_id}/children",
            json={
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
        )
        created_item_id = self._required_text(payload, "id", "Item ID")
        created_name = self._required_text(payload, "name", "nome da pasta")
        if not isinstance(payload.get("folder"), dict):
            raise OneDriveGraphError(
                "A resposta do Microsoft Graph não representa uma pasta."
            )
        parent_reference = payload.get("parentReference")
        created_drive_id = (
            self._optional_text(parent_reference.get("driveId"))
            if isinstance(parent_reference, dict)
            else None
        )
        return GraphFolder(
            item_id=created_item_id,
            name=created_name,
            drive_id=created_drive_id or drive_id,
        )

    def ensure_folder(
        self, parent_folder: GraphFolder, folder_name: str
    ) -> GraphFolder:
        existing = self.find_child_folder(parent_folder, folder_name)
        if existing is not None:
            return existing
        try:
            return self.create_folder(parent_folder, folder_name)
        except OneDriveFolderConflictError:
            existing = self.find_child_folder(parent_folder, folder_name)
            if existing is not None:
                return existing
            raise

    def ensure_folder_path(
        self, root_folder: GraphFolder, parts: Sequence[str]
    ) -> GraphFolder:
        current = root_folder
        for part in parts:
            current = self.ensure_folder(current, part)
        return current

    def rename_child_file(
        self, parent_folder: GraphFolder, current_name: str, new_name: str
    ) -> None:
        """Renomeia um arquivo existente sem overwrite ou conflito automático."""
        current = current_name.strip()
        target = new_name.strip()
        if not current or not target or any(char in target for char in "/\\"):
            raise OneDriveGraphError("Nome remoto de reparo inválido.")
        children = self.list_children(parent_folder)
        if any(
            str(item.get("name") or "").casefold() == target.casefold()
            and isinstance(item.get("file"), dict)
            for item in children
        ):
            if current.casefold() == target.casefold() or not any(
                str(item.get("name") or "").casefold() == current.casefold()
                for item in children
            ):
                return
            raise OneDriveGraphError("O nome remoto corrigido já está em uso.")
        item = next((
            item for item in children
            if str(item.get("name") or "").casefold() == current.casefold()
            and isinstance(item.get("file"), dict)
        ), None)
        if item is None:
            raise OneDriveGraphError("Arquivo remoto a renomear não foi encontrado.")
        item_id = self._required_text(item, "id", "Item ID")
        drive_id, _ = self._folder_location(parent_folder)
        endpoint = (
            f"{self.BASE_URL}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}"
        )
        try:
            response = self._session.patch(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json={"name": target}, timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError("Tempo limite ao renomear arquivo no OneDrive.") from None
        except requests.RequestException:
            raise OneDriveGraphError("Falha ao renomear arquivo no OneDrive.") from None
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response, endpoint=endpoint,
                fallback=f"Microsoft Graph recusou o rename (HTTP {response.status_code}).",
            )

    def move_child_file(
        self, parent_folder: GraphFolder, current_name: str,
        destination_folder: GraphFolder, new_name: str,
    ) -> None:
        """Move e renomeia um item por ID, sem comportamento automático de rename."""
        current = current_name.strip()
        target = new_name.strip()
        if not current or not target or any(char in target for char in "/\\"):
            raise OneDriveGraphError("Nome remoto de normalização inválido.")
        destination_children = self.list_children(destination_folder)
        if any(
            str(item.get("name") or "").casefold() == target.casefold()
            and isinstance(item.get("file"), dict)
            for item in destination_children
        ):
            source_children = self.list_children(parent_folder)
            if not any(
                str(item.get("name") or "").casefold() == current.casefold()
                and isinstance(item.get("file"), dict)
                for item in source_children
            ):
                return
            raise OneDriveGraphError("O destino remoto normalizado já está em uso.")
        source_children = self.list_children(parent_folder)
        item = next((
            child for child in source_children
            if str(child.get("name") or "").casefold() == current.casefold()
            and isinstance(child.get("file"), dict)
        ), None)
        if item is None:
            raise OneDriveGraphError("Arquivo remoto a mover não foi encontrado.")
        item_id = self._required_text(item, "id", "Item ID")
        drive_id, _ = self._folder_location(parent_folder)
        destination_drive_id, destination_item_id = self._folder_location(
            destination_folder
        )
        if destination_drive_id != drive_id:
            raise OneDriveGraphError(
                "Movimentação entre drives diferentes não é permitida pelo reparo."
            )
        endpoint = (
            f"{self.BASE_URL}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}"
        )
        try:
            response = self._session.patch(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "name": target,
                    "parentReference": {"id": destination_item_id},
                },
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError(
                "Tempo limite ao mover arquivo no OneDrive."
            ) from None
        except requests.RequestException:
            raise OneDriveGraphError("Falha ao mover arquivo no OneDrive.") from None
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response, endpoint=endpoint,
                fallback=f"Microsoft Graph recusou a movimentação (HTTP {response.status_code}).",
            )

    def upload_small_file(
        self,
        parent_folder: GraphFolder,
        local_file_path: str | Path,
        remote_filename: str | None = None,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        retry_callback: Callable[[int, int, int], None] | None = None,
    ) -> GraphUploadedItem:
        local_path = Path(local_file_path)
        if not local_path.is_file():
            raise OneDriveGraphError("Arquivo local para upload não encontrado.")
        filename = self._validated_remote_filename(local_path, remote_filename)
        try:
            file_size = local_path.stat().st_size
        except OSError:
            raise OneDriveGraphError(
                "Não foi possível ler o arquivo local para upload."
            ) from None
        if file_size > self.MAX_SMALL_UPLOAD_BYTES:
            return self.upload_large_file(
                parent_folder,
                local_path,
                remote_filename=filename,
                progress_callback=progress_callback,
                retry_callback=retry_callback,
            )
        try:
            content = local_path.read_bytes()
        except OSError:
            raise OneDriveGraphError(
                "Não foi possível ler o arquivo local para upload."
            ) from None

        drive_id, item_id = self._folder_location(parent_folder)

        encoded_drive_id = quote(drive_id, safe="")
        encoded_item_id = quote(item_id, safe="")
        encoded_filename = quote(filename, safe="")
        path = (
            f"/drives/{encoded_drive_id}/items/{encoded_item_id}:"
            f"/{encoded_filename}:/content"
        )
        endpoint = f"{self.BASE_URL}{path}"
        try:
            response = self._session.put(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": self._content_type_for_upload(local_path),
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
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback=(
                    f"Microsoft Graph retornou um erro HTTP ({response.status_code}) "
                    "durante o upload."
                ),
            )
        uploaded = self._uploaded_item(
            response, "Resposta inválida do Microsoft Graph após o upload."
        )
        if progress_callback is not None:
            progress_callback(file_size, file_size)
        return uploaded

    def upload_small_file_transactional(
        self,
        parent_folder: GraphFolder,
        local_file_path: str | Path,
        remote_filename: str | None = None,
    ) -> GraphCreatedItemReference:
        """Cria um arquivo pequeno sem overwrite e devolve identidade de rollback."""
        local_path = Path(local_file_path)
        if not local_path.is_file():
            raise OneDriveGraphError("Arquivo local para upload não encontrado.")
        filename = self._validated_remote_filename(local_path, remote_filename)
        size = local_path.stat().st_size
        if size > self.MAX_SMALL_UPLOAD_BYTES:
            raise OneDriveGraphError(
                "Upload transacional aceita somente arquivo pequeno."
            )
        drive_id, parent_id = self._folder_location(parent_folder)
        path = (
            f"/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(parent_id, safe='')}:/{quote(filename, safe='')}:/content"
            "?@microsoft.graph.conflictBehavior=fail"
        )
        endpoint = f"{self.BASE_URL}{path}"
        try:
            response = self._session.put(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": self._content_type_for_upload(local_path),
                },
                data=local_path.read_bytes(),
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError(
                "Resultado do upload transacional é indeterminado."
            ) from None
        except (OSError, requests.RequestException):
            raise OneDriveGraphError(
                "Falha no upload transacional do OneDrive."
            ) from None
        if response.status_code != 201:
            raise OneDriveGraphError(
                "Upload transacional recusado pelo Microsoft Graph.",
                http_status=response.status_code,
            )
        payload = self._response_mapping(
            response, "Resposta inválida após upload transacional."
        )
        item_id = self._required_text(payload, "id", "Item ID")
        etag = self._optional_text(payload.get("eTag"))
        if etag is None:
            metadata = self._get_item_metadata(drive_id, item_id)
            etag = self._required_text(metadata, "eTag", "eTag")
        parent = payload.get("parentReference")
        returned_drive = (
            self._optional_text(parent.get("driveId"))
            if isinstance(parent, dict) else None
        )
        if returned_drive is not None and returned_drive != drive_id:
            raise OneDriveGraphError(
                "O upload transacional retornou identidade de drive divergente."
            )
        return GraphCreatedItemReference(
            drive_id=drive_id,
            item_id=item_id,
            etag=etag,
        )

    def delete_created_item_verified(
        self,
        reference: GraphCreatedItemReference,
        *,
        max_verifications: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> GraphRollbackVerification:
        """Exclui somente item criado na transação e confirma ausência por ID."""
        if (
            not isinstance(reference, GraphCreatedItemReference)
            or not reference.created_by_transaction
            or not reference.drive_id.strip()
            or not reference.item_id.strip()
            or not reference.etag.strip()
        ):
            raise OneDriveGraphError(
                "Referência transacional incompleta ou inelegível para rollback."
            )
        checks = min(max(int(max_verifications), 1), 3)
        endpoint = self._item_endpoint(reference.drive_id, reference.item_id)
        delete_status: int | None = None
        uncertain = False
        try:
            response = self._session.delete(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "If-Match": reference.etag,
                },
                timeout=self._timeout,
            )
            delete_status = response.status_code
        except requests.Timeout:
            uncertain = True
        except requests.RequestException:
            uncertain = True
        if not uncertain and delete_status not in {204, 404}:
            raise OneDriveGraphError(
                "Exclusão compensatória recusada; revisão obrigatória.",
                http_status=delete_status,
            )
        for attempt in range(1, checks + 1):
            status, code, retry_after = self._probe_item_absence(
                reference.drive_id, reference.item_id
            )
            if status == 404 and code in {
                "itemnotfound", "resourcenotfound", "notfound",
            }:
                return GraphRollbackVerification(
                    verified_absent=True,
                    delete_status=delete_status,
                    verification_attempts=attempt,
                    delete_transport_uncertain=uncertain,
                )
            if status == 429 and attempt < checks:
                sleep(min(max(retry_after, 0.0), 2.0))
                continue
            if status in {0, 200} and attempt < checks:
                sleep(0.05)
                continue
            raise OneDriveGraphError(
                "Rollback remoto não pôde ser verificado; revisão obrigatória.",
                http_status=status,
                graph_code=code,
            )
        raise OneDriveGraphError(
            "Rollback remoto não pôde ser verificado; revisão obrigatória."
        )

    def _probe_item_absence(
        self, drive_id: str, item_id: str
    ) -> tuple[int, str | None, float]:
        endpoint = self._item_endpoint(drive_id, item_id)
        try:
            response = self._session.get(
                endpoint,
                headers={"Authorization": f"Bearer {self._access_token}"},
                params={"$select": "id,eTag"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            return 0, None, 0.0
        code = self._graph_error_code(response)
        try:
            retry_after = float(response.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        return response.status_code, code, retry_after

    def _get_item_metadata(
        self, drive_id: str, item_id: str
    ) -> dict[str, Any]:
        endpoint = self._item_endpoint(drive_id, item_id)
        try:
            response = self._session.get(
                endpoint,
                headers={"Authorization": f"Bearer {self._access_token}"},
                params={"$select": "id,eTag,parentReference"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise OneDriveGraphError(
                "Falha ao confirmar metadados do item criado."
            ) from None
        if response.status_code != 200:
            raise OneDriveGraphError(
                "Metadados do item criado não puderam ser confirmados.",
                http_status=response.status_code,
            )
        return self._response_mapping(
            response, "Metadados inválidos do item criado."
        )

    @classmethod
    def _graph_error_code(cls, response: requests.Response) -> str | None:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        value = cls._optional_text(code)
        return value.casefold() if value else None

    @staticmethod
    def _response_mapping(
        response: requests.Response, message: str
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError(message) from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError(message)
        return payload

    @classmethod
    def _item_endpoint(cls, drive_id: str, item_id: str) -> str:
        return (
            f"{cls.BASE_URL}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}"
        )

    def upload_large_file(
        self,
        parent_folder: GraphFolder,
        local_file_path: str | Path,
        remote_filename: str | None = None,
        *,
        chunk_size: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        retry_callback: Callable[[int, int, int], None] | None = None,
    ) -> GraphUploadedItem:
        """Envia arquivo por Upload Session e retoma a partir do offset remoto."""
        local_path = Path(local_file_path)
        if not local_path.is_file():
            raise OneDriveGraphError("Arquivo local para upload não encontrado.")
        filename = self._validated_remote_filename(local_path, remote_filename)
        selected_chunk_size = (
            self.large_upload_chunk_size if chunk_size is None else chunk_size
        )
        if (
            selected_chunk_size <= 0
            or selected_chunk_size % self.UPLOAD_CHUNK_ALIGNMENT != 0
        ):
            raise ValueError(
                "O tamanho do bloco deve ser um múltiplo positivo de 320 KiB."
            )
        try:
            file_size = local_path.stat().st_size
        except OSError:
            raise OneDriveGraphError(
                "Não foi possível ler o arquivo local para upload."
            ) from None

        upload_url = self._create_upload_session(parent_folder, filename)
        safe_endpoint = self._safe_upload_endpoint(upload_url)
        offset = 0
        retries = 0
        try:
            with local_path.open("rb") as stream:
                while offset < file_size:
                    stream.seek(offset)
                    chunk = stream.read(min(selected_chunk_size, file_size - offset))
                    if not chunk:
                        raise OneDriveGraphError(
                            "Não foi possível ler o próximo bloco do arquivo."
                        )
                    end = offset + len(chunk) - 1
                    try:
                        response = self._session.put(
                            upload_url,
                            headers={
                                "Content-Length": str(len(chunk)),
                                "Content-Range": f"bytes {offset}-{end}/{file_size}",
                            },
                            data=chunk,
                            timeout=self._timeout,
                        )
                    except (OSError, requests.RequestException) as exc:
                        if retries >= self.large_upload_max_retries:
                            raise OneDriveGraphError(
                                "Falha ao enviar bloco pela sessão de upload.",
                                endpoint=safe_endpoint,
                            ) from exc
                        retries += 1
                        offset = self._upload_session_offset(upload_url, file_size)
                        if retry_callback is not None:
                            retry_callback(
                                offset, retries + 1, self.large_upload_max_retries + 1
                            )
                        continue

                    if response.status_code in {200, 201}:
                        if progress_callback is not None:
                            progress_callback(file_size, file_size)
                        return self._uploaded_item(
                            response,
                            "Resposta inválida ao concluir a sessão de upload.",
                        )
                    if response.status_code == 202:
                        offset = self._next_expected_offset(response, file_size)
                        if progress_callback is not None:
                            progress_callback(offset, file_size)
                        retries = 0
                        continue
                    if response.status_code in {408, 416, 429} or response.status_code >= 500:
                        if retries < self.large_upload_max_retries:
                            retries += 1
                            offset = self._upload_session_offset(upload_url, file_size)
                            if retry_callback is not None:
                                retry_callback(
                                    offset,
                                    retries + 1,
                                    self.large_upload_max_retries + 1,
                                )
                            continue
                    raise self._response_error(
                        response,
                        endpoint=safe_endpoint,
                        fallback=(
                            "Microsoft Graph recusou um bloco da sessão de upload "
                            f"(HTTP {response.status_code})."
                        ),
                    )
        except OSError as exc:
            raise OneDriveGraphError(
                "Não foi possível ler o arquivo durante o upload em blocos."
            ) from exc
        raise OneDriveGraphError(
            "A sessão de upload terminou sem confirmar o arquivo.",
            endpoint=safe_endpoint,
        )

    def _create_upload_session(
        self, parent_folder: GraphFolder, filename: str
    ) -> str:
        drive_id, item_id = self._folder_location(parent_folder)
        path = (
            f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}:"
            f"/{quote(filename, safe='')}:/createUploadSession"
        )
        endpoint = f"{self.BASE_URL}{path}"
        try:
            response = self._session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "item": {
                        "@microsoft.graph.conflictBehavior": "fail",
                        "name": filename,
                    }
                },
                timeout=self._timeout,
            )
        except (OSError, requests.RequestException) as exc:
            raise OneDriveGraphError(
                "Falha ao criar sessão de upload no Microsoft Graph.",
                endpoint=endpoint,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback="Microsoft Graph não criou a sessão de upload.",
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        upload_url = (
            self._optional_text(payload.get("uploadUrl"))
            if isinstance(payload, dict)
            else None
        )
        if upload_url is None:
            raise OneDriveGraphError(
                "A sessão de upload não retornou uploadUrl.", endpoint=endpoint
            )
        return upload_url

    def _upload_session_offset(self, upload_url: str, file_size: int) -> int:
        safe_endpoint = self._safe_upload_endpoint(upload_url)
        try:
            response = self._session.get(upload_url, timeout=self._timeout)
        except (OSError, requests.RequestException) as exc:
            raise OneDriveGraphError(
                "Não foi possível consultar a sessão para retomada.",
                endpoint=safe_endpoint,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response,
                endpoint=safe_endpoint,
                fallback="Microsoft Graph não permitiu retomar a sessão de upload.",
            )
        return self._next_expected_offset(response, file_size)

    def _next_expected_offset(self, response: requests.Response, file_size: int) -> int:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        ranges = payload.get("nextExpectedRanges") if isinstance(payload, dict) else None
        if not isinstance(ranges, list) or not ranges:
            raise OneDriveGraphError(
                "A sessão de upload não informou nextExpectedRanges."
            )
        first_range = ranges[0]
        try:
            offset = int(str(first_range).split("-", 1)[0])
        except (TypeError, ValueError):
            raise OneDriveGraphError(
                "A sessão de upload retornou um intervalo inválido."
            ) from None
        if offset < 0 or offset > file_size:
            raise OneDriveGraphError(
                "A sessão de upload retornou um offset inválido."
            )
        return offset

    def _uploaded_item(
        self, response: requests.Response, invalid_message: str
    ) -> GraphUploadedItem:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError(invalid_message) from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError(invalid_message)
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

    @staticmethod
    def _validated_remote_filename(
        local_path: Path, remote_filename: str | None
    ) -> str:
        filename = (remote_filename or local_path.name).strip()
        if not filename:
            raise OneDriveGraphError("Nome remoto do arquivo não foi informado.")
        if "/" in filename or "\\" in filename:
            raise OneDriveGraphError("Nome remoto do arquivo é inválido.")
        return filename

    @staticmethod
    def _content_type_for_upload(local_path: Path) -> str:
        try:
            with local_path.open("rb") as stream:
                prefix = stream.read(16)
        except OSError:
            return "application/octet-stream"
        if prefix.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if prefix.startswith(b"%PDF-"):
            return "application/pdf"
        return "application/octet-stream"

    @staticmethod
    def _safe_upload_endpoint(upload_url: str) -> str:
        parts = urlsplit(upload_url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _folder_location(self, folder: GraphFolder) -> tuple[str, str]:
        if folder.is_remote:
            drive_id = folder.remote_drive_id
            item_id = folder.remote_item_id
        else:
            drive_id = folder.drive_id
            item_id = folder.item_id
        if not drive_id or not item_id:
            raise OneDriveGraphError(
                "A pasta não informou driveId e itemId para a operação."
            )
        return drive_id, item_id

    def download_json_file(
        self, parent_folder: GraphFolder, filename: str
    ) -> dict[str, Any] | None:
        """Lê um JSON remoto por nome; retorna ``None`` quando ele não existe."""
        expected = filename.strip().casefold()
        if not expected:
            raise OneDriveGraphError("Nome remoto do arquivo não foi informado.")
        item = next(
            (
                child
                for child in self.list_children(parent_folder)
                if str(child.get("name", "")).casefold() == expected
                and isinstance(child.get("file"), dict)
            ),
            None,
        )
        if item is None:
            return None
        return self.download_json_child_item(parent_folder, item)

    def download_json_child_item(
        self, parent_folder: GraphFolder, item: dict[str, Any]
    ) -> dict[str, Any]:
        """Lê JSON de um item já inventariado, sem relistar a pasta pai."""
        if not isinstance(item.get("file"), dict):
            raise OneDriveGraphError("O item remoto informado não é um arquivo.")
        item_id = self._optional_text(item.get("id"))
        if item_id is None:
            raise OneDriveGraphError("Arquivo remoto sem itemId para leitura.")
        drive_id, _ = self._folder_location(parent_folder)
        return self._get(
            f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/content",
            params={},
        )

    def _post(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.BASE_URL}{path}"
        try:
            response = self._session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json=json,
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise OneDriveGraphError(
                "Tempo limite excedido ao criar pasta no Microsoft Graph."
            ) from None
        except requests.RequestException:
            raise OneDriveGraphError(
                "Falha de comunicação ao criar pasta no Microsoft Graph."
            ) from None
        if response.status_code == 409:
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback="Já existe um item com esse nome na pasta de destino.",
                error_type=OneDriveFolderConflictError,
            )
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback=(
                    f"Microsoft Graph retornou um erro HTTP ({response.status_code}) "
                    "ao criar a pasta."
                ),
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError(
                "Resposta inválida do Microsoft Graph ao criar a pasta."
            ) from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError(
                "Resposta inválida do Microsoft Graph ao criar a pasta."
            )
        return payload

    def _get(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        endpoint = f"{self.BASE_URL}{path}"
        try:
            response = self._session.get(
                endpoint,
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
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback="A pasta raiz configurada não foi encontrada no OneDrive.",
                error_type=OneDriveRootNotFoundError,
            )
        messages = {
            401: "Autenticação rejeitada pelo Microsoft Graph (401).",
            403: "Acesso ao OneDrive negado pelo Microsoft Graph (403).",
            429: "Limite de requisições do Microsoft Graph atingido (429).",
        }
        if response.status_code in messages:
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback=messages[response.status_code],
            )
        if not 200 <= response.status_code < 300:
            raise self._response_error(
                response,
                endpoint=endpoint,
                fallback=(
                    f"Microsoft Graph retornou um erro HTTP ({response.status_code})."
                ),
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OneDriveGraphError("Resposta inválida do Microsoft Graph.") from None
        if not isinstance(payload, dict):
            raise OneDriveGraphError("Resposta inválida do Microsoft Graph.")
        return payload

    @classmethod
    def _response_error(
        cls,
        response: requests.Response,
        *,
        endpoint: str,
        fallback: str,
        error_type: type[OneDriveGraphError] = OneDriveGraphError,
    ) -> OneDriveGraphError:
        """Conserva o diagnóstico oficial do Graph sem incluir credenciais."""
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        graph_error = payload.get("error") if isinstance(payload, dict) else None
        graph_code = (
            cls._optional_text(graph_error.get("code"))
            if isinstance(graph_error, dict)
            else None
        )
        graph_message = (
            cls._optional_text(graph_error.get("message"))
            if isinstance(graph_error, dict)
            else None
        )
        inner_error = (
            graph_error.get("innerError")
            if isinstance(graph_error, dict)
            else None
        )
        headers = getattr(response, "headers", {})

        def header(name: str) -> str | None:
            value = headers.get(name) if hasattr(headers, "get") else None
            return cls._optional_text(value)

        request_id = header("request-id")
        client_request_id = header("client-request-id")
        if isinstance(inner_error, dict):
            request_id = request_id or cls._optional_text(
                inner_error.get("request-id") or inner_error.get("requestId")
            )
            client_request_id = client_request_id or cls._optional_text(
                inner_error.get("client-request-id")
                or inner_error.get("clientRequestId")
            )
        return error_type(
            graph_message or fallback,
            http_status=getattr(response, "status_code", None),
            graph_code=graph_code,
            graph_message=graph_message,
            request_id=request_id,
            client_request_id=client_request_id,
            endpoint=endpoint,
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _required_text(cls, payload: dict[str, Any], field: str, label: str) -> str:
        value = cls._optional_text(payload.get(field))
        if value is None:
            raise OneDriveGraphError(f"{label} ausente na resposta do Microsoft Graph.")
        return value


class OneDriveGraphReadOnly:
    """Visão de capacidade restrita sobre o cliente Graph autenticado."""

    __slots__ = ("__client",)

    def __init__(self, client: OneDriveGraphClient) -> None:
        self.__client = client

    def find_root_folder(self, configured_root: str) -> GraphFolder:
        return self.__client.find_root_folder(configured_root)

    def list_children(self, folder: GraphFolder) -> list[dict[str, Any]]:
        return self.__client.list_children(folder)

    def folder_from_child_item(
        self, parent_folder: GraphFolder, item: dict[str, Any]
    ) -> GraphFolder:
        return self.__client.folder_from_child_item(parent_folder, item)

    def download_json_file(
        self, parent_folder: GraphFolder, filename: str
    ) -> dict[str, Any] | None:
        return self.__client.download_json_file(parent_folder, filename)

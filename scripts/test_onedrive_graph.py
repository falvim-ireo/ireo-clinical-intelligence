"""Validação manual e somente leitura do OneDrive clínico via Microsoft Graph."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from integrations.microsoft_graph_auth import MicrosoftGraphAuth
from integrations.onedrive_graph import OneDriveGraphClient


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def list_drive_root_items(client: OneDriveGraphClient) -> list[dict[str, object]]:
    payload = client._get(
        "/me/drive/root/children",
        params={"$select": "id,name,file,folder", "$top": "100"},
    )
    values = payload.get("value")
    if not isinstance(values, list):
        raise RuntimeError("Resposta inválida ao listar a raiz do OneDrive.")
    return [item for item in values if isinstance(item, dict)]


def main() -> int:
    load_dotenv()
    auth = MicrosoftGraphAuth(
        client_id=required_env("MS_GRAPH_CLIENT_ID"),
        authority=required_env("MS_GRAPH_AUTHORITY"),
        scopes=required_env("MS_GRAPH_SCOPES"),
        token_cache_file=required_env("MS_GRAPH_TOKEN_CACHE_FILE"),
    )
    client = OneDriveGraphClient(auth.acquire_access_token())
    client.get_authenticated_user()
    client.get_drive()

    root_items = list_drive_root_items(client)
    print("Itens da raiz do OneDrive:")
    for item in root_items:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = "não informado"
        if isinstance(item.get("folder"), dict):
            item_type = "folder"
        elif isinstance(item.get("file"), dict):
            item_type = "file"
        else:
            item_type = "desconhecido"
        print(f"nome: {name}; tipo: {item_type}; ID: omitido")

    configured_root = required_env("MS_GRAPH_ONEDRIVE_ROOT")
    print(f"Localizando MS_GRAPH_ONEDRIVE_ROOT: {configured_root}")
    folder = client.find_root_folder(configured_root)
    items = client.list_first_items(folder.item_id, limit=10)

    print("Conta autenticada: sim")
    print(f"Pasta localizada: {folder.name}")
    print("Pasta raiz localizada: sim")
    print(f"Quantidade de itens retornados: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

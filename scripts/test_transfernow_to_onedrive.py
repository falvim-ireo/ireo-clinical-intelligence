"""Teste prático de download TransferNow e upload para o OneDrive."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from core.config import Config
from integrations.microsoft_graph_auth import MicrosoftGraphAuth
from integrations.onedrive_graph import OneDriveGraphClient, OneDriveGraphError
from integrations.transfernow_connector import TransferNowConnector


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESTINATION_FOLDER = "Teste TransferNow API"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def run(link: str) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    validated_link = TransferNowConnector.validar_link_download(link)
    print("Link TransferNow válido: sim")

    with TransferNowConnector.baixar_temporariamente(
        validated_link,
        connect_timeout=Config.IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS,
        read_timeout=Config.IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS,
        max_download_bytes=min(
            Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES,
            OneDriveGraphClient.MAX_SMALL_UPLOAD_BYTES,
        ),
        max_redirects=5,
    ) as downloaded:
        print("Download concluído: sim")
        print(f"Arquivo baixado: {downloaded.path.name}")

        auth = MicrosoftGraphAuth(
            client_id=required_env("MS_GRAPH_CLIENT_ID"),
            authority=required_env("MS_GRAPH_AUTHORITY"),
            scopes=required_env("MS_GRAPH_SCOPES"),
            token_cache_file=required_env("MS_GRAPH_TOKEN_CACHE_FILE"),
            output=lambda _message: None,
        )
        client = OneDriveGraphClient(auth.acquire_access_token())
        client.get_authenticated_user()
        print("Conta Microsoft autenticada: sim")

        root = client.find_root_folder(required_env("MS_GRAPH_ONEDRIVE_ROOT"))
        destination = client.ensure_folder(root, DESTINATION_FOLDER)
        print("Pasta de destino disponível: sim")

        uploaded = client.upload_small_file(destination, downloaded.path)
        print("Upload para OneDrive concluído: sim")
        if not uploaded.has_id:
            raise OneDriveGraphError(
                "O Microsoft Graph não confirmou o item remoto enviado."
            )
        print("Arquivo remoto confirmado: sim")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa um arquivo TransferNow e envia ao OneDrive."
    )
    parser.add_argument("link", help="Link público HTTPS do TransferNow")
    arguments = parser.parse_args()
    return run(arguments.link)


if __name__ == "__main__":
    raise SystemExit(main())

"""Validação manual de upload pequeno para o OneDrive via Microsoft Graph."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from dotenv import load_dotenv

from integrations.microsoft_graph_auth import MicrosoftGraphAuth
from integrations.onedrive_graph import OneDriveGraphClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_FILENAME = "teste_upload_graph.txt"
TEST_CONTENT = "Teste de upload do IREO Clinical Intelligence."


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    auth = MicrosoftGraphAuth(
        client_id=required_env("MS_GRAPH_CLIENT_ID"),
        authority=required_env("MS_GRAPH_AUTHORITY"),
        scopes=required_env("MS_GRAPH_SCOPES"),
        token_cache_file=required_env("MS_GRAPH_TOKEN_CACHE_FILE"),
        output=lambda _message: None,
    )
    client = OneDriveGraphClient(auth.acquire_access_token())
    client.get_authenticated_user()
    folder = client.find_root_folder(required_env("MS_GRAPH_ONEDRIVE_ROOT"))

    with tempfile.TemporaryDirectory(prefix="ireo_graph_upload_") as temporary_dir:
        local_file = Path(temporary_dir) / TEST_FILENAME
        local_file.write_text(TEST_CONTENT, encoding="utf-8")
        uploaded = client.upload_small_file(folder, local_file)

    print("Conta autenticada: sim")
    print(f"Pasta localizada: {folder.name}")
    print("Upload concluído: sim")
    print(f"Arquivo enviado: {uploaded.name}")
    print(f"Tamanho enviado: {uploaded.size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

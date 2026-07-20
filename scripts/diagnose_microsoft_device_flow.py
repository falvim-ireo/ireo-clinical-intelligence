"""Diagnóstico seguro do início do device flow do Microsoft Graph."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import msal

from integrations.microsoft_graph_auth import MicrosoftGraphAuth


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    )
    cache = auth._load_cache()
    app = msal.PublicClientApplication(
        client_id=auth.client_id,
        authority=auth.authority,
        token_cache=cache,
    )
    flow: Any = app.initiate_device_flow(scopes=auth.scopes)
    if not isinstance(flow, dict):
        raise RuntimeError("initiate_device_flow não retornou um dicionário")

    user_code = flow.get("user_code")
    print(f"authority: {auth.authority}")
    print(f"scopes: {auth.scopes}")
    print(f"verification_uri: {flow.get('verification_uri')}")
    if "verification_uri_complete" in flow:
        print(f"verification_uri_complete: {flow.get('verification_uri_complete')}")
    print(f"message: {flow.get('message')}")
    print(f"repr(user_code): {user_code!r}")
    print(
        "comprimento do user_code: "
        f"{len(user_code) if isinstance(user_code, str) else 0}"
    )
    print(f"chaves: {list(flow.keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

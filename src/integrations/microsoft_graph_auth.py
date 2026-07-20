"""Autenticação delegada e isolada para Microsoft Graph."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import re
from typing import Any

import msal


class MicrosoftGraphAuthError(RuntimeError):
    """Falha sanitizada durante a autenticação no Microsoft Graph."""


class MicrosoftGraphAuth:
    """Obtém tokens delegados usando cache persistente e device code flow."""

    def __init__(
        self,
        *,
        client_id: str,
        authority: str,
        scopes: str | Sequence[str],
        token_cache_file: str | Path,
        output: Callable[[str], None] = print,
        app_factory: Callable[..., Any] = msal.PublicClientApplication,
        cache_factory: Callable[[], Any] = msal.SerializableTokenCache,
    ) -> None:
        self.client_id = client_id.strip()
        self.authority = authority.strip()
        self.scopes = self._parse_scopes(scopes)
        self.token_cache_file = Path(token_cache_file).expanduser()
        self.output = output
        self._app_factory = app_factory
        self._cache_factory = cache_factory
        self._validate_configuration()

    def acquire_access_token(self) -> str:
        """Retorna um access token, tentando o cache antes do device flow."""

        cache = self._load_cache()
        try:
            app = self._app_factory(
                client_id=self.client_id,
                authority=self.authority,
                token_cache=cache,
            )
            accounts = app.get_accounts()
            result = (
                app.acquire_token_silent(self.scopes, account=accounts[0])
                if accounts
                else None
            )
            if not result or "access_token" not in result:
                flow = app.initiate_device_flow(scopes=self.scopes)
                if not isinstance(flow, dict) or "user_code" not in flow:
                    raise self._authentication_error(flow)
                message = flow.get("message")
                if isinstance(message, str) and message.strip():
                    self.output(message.strip())
                result = app.acquire_token_by_device_flow(flow)
        except MicrosoftGraphAuthError:
            raise
        except Exception as exc:
            raise self._authentication_error(exc) from None
        finally:
            self._persist_cache(cache)

        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise self._authentication_error(result)
        return token

    @classmethod
    def _authentication_error(cls, result: Any) -> MicrosoftGraphAuthError:
        if isinstance(result, dict):
            error_value = result.get("error")
            description_value = result.get("error_description")
            correlation_value = result.get("correlation_id")
        elif isinstance(result, Exception):
            error_value = getattr(result, "error", None) or type(result).__name__
            description_value = getattr(result, "error_description", None) or str(result)
            correlation_value = getattr(result, "correlation_id", None)
        else:
            error_value = description_value = correlation_value = None

        error = cls._safe_error_field(error_value, "unavailable", 128)
        description = cls._safe_error_field(
            description_value, "unavailable", 1000
        )
        correlation_id = cls._safe_correlation_id(correlation_value)
        return MicrosoftGraphAuthError(
            "Autenticação no Microsoft Graph não concluída.\n"
            f"error: {error}\n"
            f"error_description: {description}\n"
            f"correlation_id: {correlation_id}"
        )

    @staticmethod
    def _safe_error_field(value: Any, default: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            return default
        sanitized = re.sub(r"[\r\n\t]+", " ", value.strip())
        sanitized = re.sub(
            r"(?i)\b(access_token|refresh_token|client_secret|assertion|password)"
            r"\b\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*",
            "Bearer [REDACTED]",
            sanitized,
        )
        return sanitized[:limit]

    @staticmethod
    def _safe_correlation_id(value: Any) -> str:
        if not isinstance(value, str):
            return "unavailable"
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized):
            return "unavailable"
        return normalized

    def _load_cache(self) -> Any:
        cache = self._cache_factory()
        if not self.token_cache_file.exists():
            return cache
        try:
            cache.deserialize(self.token_cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MicrosoftGraphAuthError(
                "Não foi possível ler o cache de autenticação do Microsoft Graph."
            ) from None
        return cache

    def _persist_cache(self, cache: Any) -> None:
        if not getattr(cache, "has_state_changed", False):
            return
        try:
            self.token_cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.token_cache_file.with_suffix(
                self.token_cache_file.suffix + ".tmp"
            )
            temporary.write_text(cache.serialize(), encoding="utf-8")
            temporary.replace(self.token_cache_file)
            self.token_cache_file.chmod(0o600)
        except OSError:
            raise MicrosoftGraphAuthError(
                "Não foi possível persistir o cache de autenticação do Microsoft Graph."
            ) from None

    def _validate_configuration(self) -> None:
        if not self.client_id or not self.authority or not self.scopes:
            raise MicrosoftGraphAuthError(
                "Configuração de autenticação do Microsoft Graph incompleta."
            )

    @staticmethod
    def _parse_scopes(scopes: str | Sequence[str]) -> list[str]:
        if isinstance(scopes, str):
            values = scopes.replace(",", " ").split()
        else:
            values = [str(scope).strip() for scope in scopes]
        return [value for value in values if value]

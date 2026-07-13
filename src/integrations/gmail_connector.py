"""Cliente Gmail somente leitura e conversão para o modelo de domínio."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from core.config import Config
from models.email_message import EmailMessage


class GmailConnectorError(RuntimeError):
    """Falha segura ao configurar ou consultar o Gmail."""


class GmailCredentialsError(GmailConnectorError):
    """Credenciais OAuth ausentes ou inválidas."""


class GmailConnector:
    """Lê mensagens via Gmail API usando exclusivamente `gmail.readonly`."""

    SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
    PILOT_MAX_MESSAGES = 5

    def __init__(
        self,
        service: Optional[Any] = None,
        credentials_file: Optional[str | Path] = None,
        token_file: Optional[str | Path] = None,
    ) -> None:
        self._service = service
        self.credentials_file = Path(
            credentials_file or Config.GMAIL_CREDENTIALS_FILE
        )
        self.token_file = Path(token_file or Config.GMAIL_TOKEN_FILE)

    def list_messages(
        self,
        query: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> list[EmailMessage]:
        """Pesquisa e lê no máximo cinco mensagens, sem modificá-las."""

        service = self._get_service()
        safe_limit = self._safe_limit(max_results)
        try:
            response = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query or Config.GMAIL_QUERY,
                    maxResults=safe_limit,
                )
                .execute()
            )
        except Exception as exc:
            raise GmailConnectorError(
                "A consulta somente leitura ao Gmail falhou."
            ) from exc

        messages = []
        for reference in response.get("messages", []):
            message_id = str(reference.get("id", "")).strip()
            if not message_id:
                continue

            try:
                raw_message = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format="full",
                    )
                    .execute()
                )
            except Exception as exc:
                raise GmailConnectorError(
                    "A leitura de uma mensagem do Gmail falhou."
                ) from exc
            messages.append(self.parse_message(raw_message))

        return messages

    @classmethod
    def parse_message(cls, raw_message: dict[str, Any]) -> EmailMessage:
        """Converte a resposta MIME do Gmail em `EmailMessage`."""

        payload = raw_message.get("payload") or {}
        headers = cls._headers(payload)
        text_parts, html_parts = cls._body_parts(payload)

        return EmailMessage(
            message_id=str(raw_message.get("id", "")).strip(),
            subject=cls._decode_header(headers.get("subject", "")),
            sender=cls._decode_header(headers.get("from", "")),
            reply_to=(
                cls._decode_header(headers["reply-to"])
                if headers.get("reply-to")
                else None
            ),
            recipients=[
                address
                for _, address in getaddresses([headers.get("to", "")])
                if address
            ],
            received_at=cls._received_at(
                headers.get("date"),
                raw_message.get("internalDate"),
            ),
            text_body="\n".join(text_parts).strip(),
            html_body="\n".join(html_parts).strip(),
        )

    def _get_service(self) -> Any:
        if self._service is None:
            self._service = self._authenticate()
        return self._service

    def _authenticate(self) -> Any:
        """Executa OAuth desktop e cria um cliente Gmail somente leitura."""

        if not self.token_file.exists() and not self.credentials_file.exists():
            raise GmailCredentialsError(
                "Credenciais OAuth do Gmail não foram encontradas."
            )

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailConnectorError(
                "As bibliotecas oficiais do Gmail não estão instaladas."
            ) from exc

        credentials = None
        if self.token_file.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self.token_file),
                    self.SCOPES,
                )
            except (OSError, ValueError) as exc:
                raise GmailCredentialsError(
                    "O token OAuth do Gmail não pôde ser carregado."
                ) from exc

        if not credentials or not credentials.valid:
            try:
                if (
                    credentials
                    and credentials.expired
                    and credentials.refresh_token
                ):
                    credentials.refresh(Request())
                else:
                    if not self.credentials_file.exists():
                        raise GmailCredentialsError(
                            "Credenciais do aplicativo desktop não foram "
                            "encontradas."
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.credentials_file),
                        self.SCOPES,
                    )
                    credentials = flow.run_local_server(port=0)
            except GmailCredentialsError:
                raise
            except Exception as exc:
                raise GmailConnectorError(
                    "A autorização OAuth do Gmail não foi concluída."
                ) from exc

            try:
                self.token_file.write_text(
                    credentials.to_json(),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise GmailCredentialsError(
                    "O token OAuth do Gmail não pôde ser armazenado."
                ) from exc

        try:
            return build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
        except Exception as exc:
            raise GmailConnectorError(
                "O cliente somente leitura do Gmail não pôde ser criado."
            ) from exc

    @classmethod
    def _safe_limit(cls, value: Optional[int]) -> int:
        requested = Config.GMAIL_MAX_MESSAGES if value is None else value
        try:
            parsed = int(requested)
        except (TypeError, ValueError):
            parsed = Config.GMAIL_MAX_MESSAGES
        return min(max(parsed, 1), cls.PILOT_MAX_MESSAGES)

    @staticmethod
    def _headers(payload: dict[str, Any]) -> dict[str, str]:
        return {
            str(header.get("name", "")).casefold(): str(
                header.get("value", "")
            )
            for header in payload.get("headers", [])
            if header.get("name")
        }

    @classmethod
    def _body_parts(
        cls,
        payload: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        text_parts: list[str] = []
        html_parts: list[str] = []

        mime_type = str(payload.get("mimeType", "")).casefold()
        encoded_data = (payload.get("body") or {}).get("data")
        if encoded_data and mime_type in {"text/plain", "text/html"}:
            decoded = cls._decode_body(str(encoded_data))
            if mime_type == "text/plain":
                text_parts.append(decoded)
            else:
                html_parts.append(decoded)

        for part in payload.get("parts") or []:
            child_text, child_html = cls._body_parts(part)
            text_parts.extend(child_text)
            html_parts.extend(child_html)

        return text_parts, html_parts

    @staticmethod
    def _decode_body(encoded_data: str) -> str:
        padding = "=" * (-len(encoded_data) % 4)
        try:
            content = base64.urlsafe_b64decode(encoded_data + padding)
        except (ValueError, TypeError) as exc:
            raise GmailConnectorError(
                "Uma parte MIME do Gmail não pôde ser decodificada."
            ) from exc
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except (LookupError, UnicodeError):
            return value

    @staticmethod
    def _received_at(
        date_header: Optional[str],
        internal_date: Optional[str],
    ) -> datetime:
        if date_header:
            try:
                parsed = parsedate_to_datetime(date_header)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except (TypeError, ValueError, OverflowError):
                pass

        try:
            milliseconds = int(internal_date or 0)
        except (TypeError, ValueError):
            milliseconds = 0
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)

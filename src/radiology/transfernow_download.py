"""Download HTTP supervisionado e restrito ao ecossistema TransferNow."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit

import requests


class TransferNowDownloadError(RuntimeError):
    """Falha segura, sem expor URL, token ou headers."""


class BrowserInteractionRequired(TransferNowDownloadError):
    """O link não oferece endpoint HTTP confiável."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    sha256: str
    reused: bool = False


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.links.append(value)


class TransferNowDownloader:
    USER_AGENT = "IREO-Clinical-Intelligence/0.1 supervised-download"
    EXTENSIONS = {".rar", ".zip"}
    MAX_FILENAME_LENGTH = 120

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        connect_timeout: int = 30,
        read_timeout: int = 1800,
        max_download_bytes: int = 10_737_418_240,
        max_redirects: int = 5,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = (connect_timeout, read_timeout)
        self.max_download_bytes = max_download_bytes
        self.max_redirects = max_redirects

    def download(
        self,
        url: str,
        original_filename: str | None,
        quarantine_root: str | Path,
        correlation_id: str,
        message_id: str,
    ) -> DownloadResult:
        self._validate_url(url)
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", correlation_id):
            raise TransferNowDownloadError("Correlation ID inválido.")
        quarantine = Path(quarantine_root).expanduser().resolve()
        folder = quarantine / correlation_id
        if not folder.resolve().is_relative_to(quarantine):
            raise TransferNowDownloadError("Destino de quarentena inválido.")
        folder.mkdir(parents=True, exist_ok=True)
        response, final_url = self._request(url)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type in {"text/html", "application/xhtml+xml"}:
            endpoint = self._endpoint_from_html(response.text, final_url)
            response.close()
            if not endpoint:
                raise BrowserInteractionRequired(
                    "O link exige interação de navegador; use --archive-path."
                )
            response, final_url = self._request(endpoint)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type in {"text/html", "application/xhtml+xml"}:
                response.close()
                raise BrowserInteractionRequired(
                    "O link exige interação de navegador; use --archive-path."
                )

        disposition_name = self._content_disposition_filename(
            response.headers.get("Content-Disposition", "")
        )
        url_name = unquote(Path(urlsplit(final_url).path).name)
        filename = None
        for candidate in (disposition_name, original_filename, url_name):
            if not candidate:
                continue
            try:
                filename = self._safe_filename(candidate)
                break
            except TransferNowDownloadError:
                continue
        if filename is None:
            response.close()
            raise TransferNowDownloadError("Nome de arquivo inválido.")

        final_path = folder / filename
        part_path = folder / f"{filename}.part"
        metadata_path = folder / f"{filename}.download.json"
        reused = self._completed_download(metadata_path, final_path, message_id, filename)
        if reused:
            response.close()
            return reused
        if final_path.exists() or part_path.exists():
            response.close()
            raise TransferNowDownloadError(
                "Já existe arquivo relacionado; revisão humana obrigatória."
            )

        expected_length = self._content_length(response.headers.get("Content-Length"))
        if expected_length is not None and expected_length > self.max_download_bytes:
            response.close()
            raise TransferNowDownloadError("O arquivo excede o limite configurado.")

        digest = hashlib.sha256()
        total = 0
        try:
            with part_path.open("xb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise TransferNowDownloadError("O arquivo excede o limite configurado.")
                    output.write(chunk)
                    digest.update(chunk)
        except (requests.RequestException, OSError):
            self._write_failed(metadata_path, message_id, filename, total)
            raise TransferNowDownloadError("O download foi interrompido.") from None
        except TransferNowDownloadError:
            self._write_failed(metadata_path, message_id, filename, total)
            raise
        finally:
            response.close()

        if total == 0:
            self._write_failed(metadata_path, message_id, filename, total)
            raise TransferNowDownloadError("O download retornou um arquivo vazio.")
        if expected_length is not None and total != expected_length:
            self._write_failed(metadata_path, message_id, filename, total)
            raise TransferNowDownloadError("O download ficou incompleto.")

        checksum = digest.hexdigest()
        part_path.replace(final_path)
        metadata_path.write_text(
            json.dumps(
                {
                    "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                    "filename": filename,
                    "size_bytes": total,
                    "sha256": checksum,
                    "status": "COMPLETED",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return DownloadResult(final_path, total, checksum)

    def _request(self, url: str):
        current = url
        for _ in range(self.max_redirects + 1):
            self._validate_url(current)
            try:
                response = self.session.get(
                    current,
                    allow_redirects=False,
                    stream=True,
                    timeout=self.timeout,
                    headers={"User-Agent": self.USER_AGENT},
                )
            except requests.RequestException:
                raise TransferNowDownloadError("Falha HTTP no download.") from None
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise TransferNowDownloadError("Redirect inválido.")
                current = urljoin(current, location)
                self._validate_url(current)
                continue
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise TransferNowDownloadError("Status HTTP inválido no download.") from None
            return response, current
        raise TransferNowDownloadError("Limite de redirects excedido.")

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            raise TransferNowDownloadError("Link TransferNow inválido.") from None
        if parsed.scheme.casefold() != "https" or not (
            hostname == "transfernow.net" or hostname.endswith(".transfernow.net")
        ):
            raise TransferNowDownloadError("Domínio de download não permitido.")

    @classmethod
    def _safe_filename(cls, value: str) -> str:
        decoded = unquote(str(value or ""))
        if (
            not decoded
            or "/" in decoded
            or "\\" in decoded
            or decoded.startswith("..")
            or "..." in decoded
        ):
            raise TransferNowDownloadError("Nome de arquivo inválido.")
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", decoded).rstrip(" .")
        if not cleaned or Path(cleaned).suffix.casefold() not in cls.EXTENSIONS:
            raise TransferNowDownloadError("Extensão de arquivo não permitida.")
        suffix = Path(cleaned).suffix
        # Reserva espaço para os sufixos internos ".part" e ".download.json".
        if len(cleaned) > cls.MAX_FILENAME_LENGTH:
            stem = cleaned[:-len(suffix)].rstrip(" .")
            stem_limit = cls.MAX_FILENAME_LENGTH - len(suffix)
            cleaned = f"{stem[:stem_limit].rstrip(' .')}{suffix}"
        if not cleaned[:-len(suffix)].rstrip(" ."):
            raise TransferNowDownloadError("Nome de arquivo inválido.")
        return cleaned

    @staticmethod
    def _content_length(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            raise TransferNowDownloadError("Content-Length inválido.") from None
        if parsed < 0:
            raise TransferNowDownloadError("Content-Length inválido.")
        return parsed

    @staticmethod
    def _content_disposition_filename(value: str) -> str | None:
        if not value:
            return None
        message = Message()
        message["Content-Disposition"] = value
        filename = message.get_filename()
        return unquote(filename).strip() if filename else None

    def _endpoint_from_html(self, body: str, base_url: str) -> str | None:
        parser = _LinkParser()
        parser.feed(body)
        for link in parser.links:
            candidate = urljoin(base_url, link)
            try:
                self._validate_url(candidate)
            except TransferNowDownloadError:
                continue
            if re.search(r"download|/dl/", urlsplit(candidate).path, re.IGNORECASE):
                return candidate
        return None

    @staticmethod
    def _completed_download(metadata_path, final_path, message_id, filename):
        if not metadata_path.is_file() or not final_path.is_file():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            checksum = hashlib.sha256()
            with final_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
            message_hash = hashlib.sha256(message_id.encode()).hexdigest()
            if (
                data.get("status") == "COMPLETED"
                and data.get("message_id_sha256") == message_hash
                and data.get("filename") == filename
                and data.get("size_bytes") == final_path.stat().st_size
                and data.get("sha256") == digest
            ):
                return DownloadResult(final_path, final_path.stat().st_size, digest, True)
        except (OSError, ValueError, TypeError):
            return None
        return None

    @staticmethod
    def _write_failed(metadata_path, message_id, filename, size_bytes) -> None:
        try:
            metadata_path.write_text(
                json.dumps(
                    {
                        "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                        "filename": filename,
                        "size_bytes": size_bytes,
                        "status": "FAILED",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

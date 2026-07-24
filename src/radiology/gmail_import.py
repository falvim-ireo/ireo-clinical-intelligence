"""Orquestra seleção readonly e download supervisionado do TransferNow."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import re
from typing import Callable
from urllib.parse import urlsplit

from integrations.gmail_connector import GmailConnector
from integrations.transfernow_connector import TransferNowConnector, TransferNowMessage
from observability.audit_logger import mask_message_id
from radiology.transfernow_download import DownloadResult, TransferNowDownloader
from radiology.transfernow_download import BrowserInteractionRequired


@dataclass(frozen=True)
class GmailImportOutcome:
    download: DownloadResult
    import_result: object | None


def run_gmail_import(
    *,
    gmail: GmailConnector,
    downloader: TransferNowDownloader,
    importer,
    quarantine_root,
    correlation_id: str,
    browser_downloader=None,
    input_func: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> GmailImportOutcome:
    messages = gmail.list_messages(
        query="from:noreply@transfernow.net subject:TransferNow",
        max_results=5,
    )
    parsed: list[tuple[object, TransferNowMessage, str | None, str | None]] = []
    public_link_missing = False
    for message in messages:
        body = "\n".join(value for value in (message.text_body, message.html_body) if value)
        try:
            transfer = TransferNowConnector.interpretar(body, message.subject)
        except ValueError as exc:
            if "link público de transferência válido" in str(exc):
                public_link_missing = True
            continue
        size = _first_match(body, r"(?:tamanho|size)\s*:?\s*([0-9.,]+\s*(?:KB|MB|GB|TB))")
        validity = _first_match(body, r"(?:v[aá]lid[oa]|expires?)\s*:?\s*([^\n<]{1,60})")
        if validity:
            validity = re.split(r"https?://", validity, maxsplit=1)[0].strip(" .")
        parsed.append((message, transfer, size, validity))

    if not parsed:
        if public_link_missing:
            raise ValueError(
                "O e-mail não forneceu um link público de transferência válido."
            )
        raise ValueError("Nenhuma mensagem TransferNow utilizável foi encontrada.")
    output("Mensagens TransferNow recentes:")
    for index, (message, transfer, size, _) in enumerate(parsed, 1):
        output(
            f"{index}. Data: {message.received_at:%Y-%m-%d %H:%M}; "
            f"Remetente: {transfer.sender_email or 'não informado'}; "
            f"Arquivo: {transfer.display_filename or 'não informado'}; "
            f"Tamanho: {size or 'não informado'}; "
            f"Message ID: {mask_message_id(message.message_id)}"
        )
    selected = (
        1
        if len(parsed) == 1
        else _choice(input_func("Selecione o número da mensagem: "), len(parsed))
    )
    message, transfer, _, validity = parsed[selected - 1]
    if not transfer.original_filename:
        raise ValueError("A mensagem selecionada não informa o arquivo esperado.")
    precheck = getattr(importer, "check_message_duplicate", None)
    if callable(precheck):
        precheck(message.message_id, transfer.download_url)
    output("Gmail................. OK")
    output(f"Arquivo esperado: {transfer.display_filename}")
    output(f"Domínio: {urlsplit(transfer.download_url).hostname}")
    output(f"Validade: {validity or 'não informada'}")
    try:
        downloaded = downloader.download(
            transfer.download_url,
            transfer.original_filename,
            quarantine_root,
            correlation_id,
            message.message_id,
        )
    except BrowserInteractionRequired:
        if browser_downloader is None:
            raise
        output(f"Domínio: {urlsplit(transfer.download_url).hostname}")
        output(f"Arquivo esperado: {transfer.display_filename}")
        output("Navegador............ ABRINDO")
        output("Nenhuma alteração será feita no Gmail.")
        downloaded = browser_downloader.download(
            transfer.download_url,
            transfer.original_filename,
            quarantine_root,
            correlation_id,
            message.message_id,
        )
    display_downloaded = TransferNowConnector._nome_arquivo_para_exibicao(downloaded.path.name)
    output(f"Nome: {display_downloaded}")
    output(f"Tamanho: {downloaded.size_bytes} bytes")
    output(f"SHA-256: {downloaded.sha256}")
    output(f"Caminho na quarentena: {downloaded.path}")
    output("Download.............. OK")
    intake_record_id = None
    register_download = getattr(importer, "register_download", None)
    if callable(register_download):
        intake_record_id = register_download(
            archive_path=downloaded.path,
            archive_sha256=downloaded.sha256,
            gmail_message_id=message.message_id,
            transfer_url=transfer.download_url,
        )
    return GmailImportOutcome(
        downloaded,
        _run_supervised_import(
            importer, downloaded, message.message_id, transfer.download_url,
            intake_record_id, message.received_at,
        ),
    )


def _choice(value: str, maximum: int) -> int:
    try:
        selected = int(value.strip())
    except ValueError:
        raise ValueError("Seleção inválida.") from None
    if not 1 <= selected <= maximum:
        raise ValueError("Seleção inválida.")
    return selected


def _run_supervised_import(
    importer, downloaded, message_id: str, transfer_url: str,
    intake_record_id: int | None, email_received_at=None,
):
    parameters = inspect.signature(importer.run).parameters
    accepts_metadata = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    supports_metadata = accepts_metadata or "gmail_message_id" in parameters
    if not supports_metadata:
        return importer.run(archive_path=downloaded.path)
    metadata = dict(
        archive_path=downloaded.path,
        gmail_message_id=message_id,
        transfer_url=transfer_url,
        archive_sha256=downloaded.sha256,
        intake_record_id=intake_record_id,
        email_received_at=email_received_at,
    )
    if not accepts_metadata:
        metadata = {key: value for key, value in metadata.items() if key in parameters}
    return importer.run(**metadata)


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None

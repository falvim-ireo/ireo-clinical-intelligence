"""Diagnóstico readonly e sanitizado de links TransferNow em uma mensagem."""

from __future__ import annotations

from typing import Callable

from integrations.transfernow_connector import TransferNowConnector


def run_transfernow_link_diagnosis(
    *, gmail, input_func: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> None:
    messages = gmail.list_messages(
        query="from:noreply@transfernow.net subject:TransferNow", max_results=5
    )
    if not messages:
        raise ValueError("Nenhuma mensagem TransferNow foi encontrada.")
    output(f"Mensagens disponíveis: {len(messages)}")
    try:
        selected = int(input_func("Selecione o número da mensagem: ").strip())
    except ValueError:
        raise ValueError("Seleção inválida.") from None
    if not 1 <= selected <= len(messages):
        raise ValueError("Seleção inválida.")
    message = messages[selected - 1]
    body = "\n".join(
        value for value in (message.text_body, message.html_body) if value
    )
    diagnosis = TransferNowConnector.diagnosticar_links(body)
    output(f"Links encontrados: {diagnosis.total_links}")
    output(f"Links TransferNow: {diagnosis.transfernow_links}")
    output(f"Candidatos públicos: {diagnosis.public_candidates}")
    output(f"Candidatos /dl/: {diagnosis.dl_candidates}")
    output(f"Tipo escolhido: {diagnosis.candidate_type or 'nenhum'}")
    output(f"Texto associado: {diagnosis.candidate_text or 'não informado'}")
    output(f"Hostname: {diagnosis.hostname or 'nenhum'}")
    output(f"Presença de path: {'sim' if diagnosis.has_path else 'não'}")
    output(f"Presença de query: {'sim' if diagnosis.has_query else 'não'}")

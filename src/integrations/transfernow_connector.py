"""
Integração responsável por interpretar mensagens do TransferNow.

Este módulo não realiza o download dos arquivos. Ele apenas extrai,
valida e organiza as informações contidas no assunto e no corpo
do e-mail recebido.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


class _TransferNowHTMLParser(HTMLParser):
    """Coleta texto visível e links sem descartar atributos href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        for nome, valor in attrs:
            if nome.lower() == "href" and valor:
                self.partes.append(valor)

    def handle_data(self, data: str) -> None:
        if data:
            self.partes.append(data)


@dataclass(frozen=True)
class TransferNowMessage:
    """Representa os dados extraídos de um e-mail do TransferNow."""

    download_url: str
    filename: Optional[str]
    patient_name_candidate: Optional[str]
    sender_email: Optional[str]
    sender_name: Optional[str] = None


class TransferNowConnector:
    """Interpreta mensagens recebidas por meio do TransferNow."""

    _URL_PATTERN = re.compile(
        r"https://[^\s<>'\"]+",
        flags=re.IGNORECASE,
    )

    _FILENAME_PATTERN = re.compile(
        r'["“](?P<filename>[^"”]+\.(?:rar|zip|7z))["”]',
        flags=re.IGNORECASE,
    )

    _EMAIL_PATTERN = re.compile(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        flags=re.IGNORECASE,
    )

    _SUPPORTED_EXTENSIONS = (".rar", ".zip", ".7z")

    @classmethod
    def interpretar(
        cls,
        conteudo: str,
        assunto: str = "",
    ) -> TransferNowMessage:
        """
        Interpreta o assunto e o corpo de uma mensagem do TransferNow.

        Args:
            conteudo: Corpo da mensagem, em texto simples ou HTML.
            assunto: Assunto do e-mail.

        Returns:
            Objeto TransferNowMessage com os dados encontrados.

        Raises:
            ValueError: Quando não for encontrado um link válido.
        """

        texto_normalizado = cls._normalizar_conteudo(conteudo)

        download_url = cls._extrair_url(texto_normalizado)

        if not download_url:
            raise ValueError(
                "Nenhum link válido do TransferNow foi encontrado."
            )

        filename = (
            cls._extrair_nome_arquivo(assunto)
            or cls._extrair_nome_arquivo(texto_normalizado)
        )

        patient_name_candidate = cls._extrair_nome_paciente(
            filename
        )

        sender_email = cls._extrair_email_remetente(
            texto_normalizado
        )

        return TransferNowMessage(
            download_url=download_url,
            filename=filename,
            patient_name_candidate=patient_name_candidate,
            sender_email=sender_email,
        )

    @classmethod
    def eh_mensagem_transfernow(
        cls,
        conteudo: str,
        assunto: str = "",
    ) -> bool:
        """Verifica se o conteúdo contém indícios de TransferNow."""

        texto = cls._normalizar_conteudo(
            f"{assunto} {conteudo}"
        )

        return cls._extrair_url(texto) is not None

    @staticmethod
    def _normalizar_conteudo(conteudo: str) -> str:
        """Converte HTML básico em texto e normaliza espaços."""

        if not conteudo:
            return ""

        parser = _TransferNowHTMLParser()
        parser.feed(conteudo)
        parser.close()

        texto = re.sub(
            r"\s+",
            " ",
            " ".join(parser.partes),
        )

        return html.unescape(texto).strip()

    @classmethod
    def _extrair_url(
        cls,
        texto: str,
    ) -> Optional[str]:
        """Extrai e higieniza a primeira URL válida do TransferNow."""

        for correspondencia in cls._URL_PATTERN.finditer(texto):
            url = correspondencia.group(0).rstrip(
                ".,);]}>"
            )

            try:
                partes = urlsplit(url)
                hostname = (partes.hostname or "").lower().rstrip(".")
            except ValueError:
                continue

            dominio_valido = (
                hostname == "transfernow.net"
                or hostname.endswith(".transfernow.net")
            )

            if partes.scheme.lower() != "https" or not dominio_valido:
                continue

            return urlunsplit(
                (
                    partes.scheme,
                    partes.netloc,
                    partes.path,
                    partes.query,
                    "",
                )
            )

        return None

    @classmethod
    def _extrair_nome_arquivo(
        cls,
        texto: str,
    ) -> Optional[str]:
        """Extrai o nome do arquivo compactado."""

        if not texto:
            return None

        correspondencia = cls._FILENAME_PATTERN.search(texto)

        if correspondencia:
            return correspondencia.group("filename").strip()

        palavras = re.findall(
            r"[^\s<>\"']+\.(?:rar|zip|7z)",
            texto,
            flags=re.IGNORECASE,
        )

        if not palavras:
            return None

        return palavras[0].strip(".,);]}>")

    @classmethod
    def _extrair_email_remetente(
        cls,
        texto: str,
    ) -> Optional[str]:
        """Extrai o provável e-mail da clínica remetente."""

        emails = cls._EMAIL_PATTERN.findall(texto)

        for email_encontrado in emails:
            email_normalizado = email_encontrado.lower()

            if "transfernow.net" not in email_normalizado:
                return email_encontrado

        return None

    @classmethod
    def _extrair_nome_paciente(
        cls,
        filename: Optional[str],
    ) -> Optional[str]:
        """
        Extrai o provável nome do paciente a partir do arquivo.

        Exemplo:
            VERA LUCIA CRUZ DA SILVA_20260711.rar

        Resultado:
            VERA LUCIA CRUZ DA SILVA
        """

        if not filename:
            return None

        nome_sem_extensao = filename

        for extensao in cls._SUPPORTED_EXTENSIONS:
            if nome_sem_extensao.lower().endswith(extensao):
                nome_sem_extensao = nome_sem_extensao[
                    : -len(extensao)
                ]
                break

        candidato = re.split(
            r"[_\-\s]\d{4,}",
            nome_sem_extensao,
            maxsplit=1,
        )[0]

        candidato = candidato.replace("_", " ")
        candidato = re.sub(r"\s+", " ", candidato).strip()

        return candidato or None

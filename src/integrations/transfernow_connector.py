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
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


@dataclass(frozen=True)
class TransferNowLinkDiagnosis:
    total_links: int
    transfernow_links: int
    public_candidates: int
    dl_candidates: int = 0
    candidate_type: Optional[str] = None
    candidate_text: Optional[str] = None
    hostname: Optional[str] = None
    has_path: bool = False
    has_query: bool = False


@dataclass(frozen=True)
class _EmailLink:
    url: str
    text: str = ""
    source: str = "href"


class _TransferNowHTMLParser(HTMLParser):
    """Coleta texto visível e links sem descartar atributos href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self.links: list[_EmailLink] = []
        self._active_href: Optional[str] = None
        self._active_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        href = None
        for nome, valor in attrs:
            if nome.lower() == "href" and valor:
                self.partes.append(valor)
                href = html.unescape(valor).strip()
        if href:
            if tag.casefold() == "a":
                self._active_href = href
                self._active_text = []
            else:
                self.links.append(_EmailLink(href, source=f"href:{tag.casefold()}"))

    def handle_data(self, data: str) -> None:
        if data:
            self.partes.append(data)
            if self._active_href is not None:
                self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._active_href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._active_text)).strip()
            self.links.append(_EmailLink(self._active_href, text, "href:a"))
            self._active_href = None
            self._active_text = []


@dataclass(frozen=True)
class TransferNowMessage:
    """Representa os dados extraídos de um e-mail do TransferNow."""

    download_url: str
    original_filename: Optional[str]
    display_filename: Optional[str]
    patient_name_candidate: Optional[str]
    sender_email: Optional[str]
    sender_name: Optional[str] = None
    link_diagnosis: Optional[TransferNowLinkDiagnosis] = None

    @property
    def filename(self) -> Optional[str]:
        """Compatibilidade: o nome de arquivo sempre significa o valor original."""

        return self.original_filename


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
    def extrair_nome_paciente_do_arquivo(
        cls,
        filename: Optional[str],
    ) -> Optional[str]:
        """Expõe a identificação provável usada pelo fluxo supervisionado."""

        return cls._extrair_nome_paciente(filename)

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

        download_url, link_diagnosis = cls._selecionar_url_publica(conteudo)

        if not download_url:
            if link_diagnosis.transfernow_links:
                raise ValueError(
                    "O e-mail não forneceu um link público de transferência válido."
                )
            raise ValueError("Nenhum link válido do TransferNow foi encontrado.")

        original_filename = (
            cls._extrair_nome_arquivo(assunto)
            or cls._extrair_nome_arquivo(texto_normalizado)
        )

        patient_name_candidate = cls._extrair_nome_paciente(
            original_filename
        )

        sender_email = cls._extrair_email_remetente(
            texto_normalizado
        )

        return TransferNowMessage(
            download_url=download_url,
            original_filename=original_filename,
            display_filename=cls._nome_arquivo_para_exibicao(original_filename),
            patient_name_candidate=patient_name_candidate,
            sender_email=sender_email,
            link_diagnosis=link_diagnosis,
        )

    @staticmethod
    def _nome_arquivo_para_exibicao(filename: Optional[str], limit: int = 36) -> Optional[str]:
        """Mascara nomes longos; este valor existe exclusivamente para o terminal."""

        if not filename or len(filename) <= limit:
            return filename
        suffix = next(
            (extension for extension in (".rar", ".zip", ".7z") if filename.casefold().endswith(extension)),
            "",
        )
        stem = filename[:-len(suffix)] if suffix else filename
        visible = max(1, limit - len(suffix) - len("... "))
        return f"{stem[:visible]}... {suffix}"

    @classmethod
    def eh_mensagem_transfernow(
        cls,
        conteudo: str,
        assunto: str = "",
    ) -> bool:
        """Verifica se o conteúdo contém indícios de TransferNow."""

        diagnosis = cls.diagnosticar_links(f"{assunto} {conteudo}")
        return diagnosis.transfernow_links > 0

    @classmethod
    def diagnosticar_links(cls, conteudo: str) -> TransferNowLinkDiagnosis:
        _, diagnosis = cls._selecionar_url_publica(conteudo)
        return diagnosis

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

        selected, _ = cls._selecionar_url_publica(texto)
        return selected

    @classmethod
    def _selecionar_url_publica(
        cls, conteudo: str
    ) -> tuple[Optional[str], TransferNowLinkDiagnosis]:
        links = cls._coletar_links(conteudo)
        candidates_by_url: dict[str, tuple[int, str, str, Optional[str]]] = {}
        transfer_urls: set[str] = set()
        for link in links:
            for url, candidate_type in cls._urls_transfernow(link):
                normalized = cls._normalizar_url(url)
                if not normalized:
                    continue
                transfer_urls.add(normalized)
                if not cls._eh_link_publico(normalized):
                    continue
                parsed = urlsplit(normalized)
                semantic_text = cls._texto_semantico(link.text)
                is_dl = parsed.path.casefold().startswith("/dl/")
                if is_dl and semantic_text == "Acessar o download":
                    score = 100
                elif is_dl:
                    score = 95
                elif semantic_text and parsed.path not in {"", "/"}:
                    score = 80
                else:
                    score = 50
                kind = "link /dl/" if is_dl else (
                    "botão semântico" if semantic_text else candidate_type
                )
                candidate = (score, normalized, kind, semantic_text)
                previous = candidates_by_url.get(normalized)
                if previous is None or candidate[0] > previous[0]:
                    candidates_by_url[normalized] = candidate
        candidates = sorted(
            candidates_by_url.values(), key=lambda item: item[0], reverse=True
        )
        chosen = candidates[0] if candidates else None
        parsed = urlsplit(chosen[1]) if chosen else None
        diagnosis = TransferNowLinkDiagnosis(
            total_links=len(links),
            transfernow_links=len(transfer_urls),
            public_candidates=len(candidates),
            dl_candidates=sum(
                urlsplit(candidate[1]).path.casefold().startswith("/dl/")
                for candidate in candidates
            ),
            candidate_type=chosen[2] if chosen else None,
            candidate_text=chosen[3] if chosen else None,
            hostname=parsed.hostname if parsed else None,
            has_path=bool(parsed and parsed.path not in {"", "/"}),
            has_query=bool(parsed and parsed.query),
        )
        return (chosen[1] if chosen else None), diagnosis

    @staticmethod
    def _texto_semantico(value: str) -> Optional[str]:
        normalized = re.sub(r"\s+", " ", value or "").strip().casefold()
        labels = (
            ("acessar o download", "Acessar o download"),
            ("acessar arquivos", "Acessar arquivos"),
            ("obter arquivos", "Obter arquivos"),
            ("download", "Download"),
            ("baixar", "Baixar"),
            ("view transfer", "View transfer"),
            ("get your files", "Get your files"),
        )
        for fragment, safe_label in labels:
            if fragment in normalized:
                return safe_label
        return None

    @classmethod
    def _coletar_links(cls, conteudo: str) -> list[_EmailLink]:
        parser = _TransferNowHTMLParser()
        parser.feed(conteudo or "")
        parser.close()
        links = list(parser.links)
        href_urls = {link.url for link in links}
        for match in cls._URL_PATTERN.finditer(html.unescape(conteudo or "")):
            url = match.group(0).rstrip(".,);]}>")
            if url not in href_urls:
                links.append(_EmailLink(url, source="texto"))
        return links

    @classmethod
    def _urls_transfernow(cls, link: _EmailLink):
        if cls._normalizar_url(link.url):
            yield link.url, "link TransferNow"
        try:
            query_values = parse_qsl(urlsplit(link.url).query, keep_blank_values=False)
        except ValueError:
            return
        for _, value in query_values:
            decoded = unquote(html.unescape(value))
            if cls._normalizar_url(decoded):
                yield decoded, "redirecionamento rastreado"

    @staticmethod
    def _normalizar_url(url: str) -> Optional[str]:
        try:
            parsed = urlsplit(html.unescape(url).strip())
            hostname = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            return None
        if parsed.scheme.casefold() != "https" or not (
            hostname == "transfernow.net" or hostname.endswith(".transfernow.net")
        ):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))

    @staticmethod
    def _eh_link_publico(url: str) -> bool:
        parsed = urlsplit(url)
        path = (parsed.path or "/").casefold().rstrip("/") or "/"
        rejected = {"/login", "/signin", "/privacy", "/terms", "/support"}
        if path == "/":
            return bool(parsed.query)
        return path not in rejected

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

"""Inspeciona a entrega HTTP do TransferNow sem baixar o arquivo completo."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import re
import tempfile
from urllib.parse import urljoin, urlsplit

import requests


MAX_HTML_BYTES = 10 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "IREO-Clinical-Intelligence/transfernow-inspection"
API_PATTERN = re.compile(r"(?P<endpoint>/api/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)")
JS_URL_PATTERN = re.compile(
    r"(?:url|uri|endpoint|downloadUrl|download_url)\s*[:=]\s*"
    r"[\"'](?P<url>(?:https?://|/)[^\"']+)[\"']",
    re.IGNORECASE,
)


class InspectionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.data_attributes: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {
            str(name).casefold(): str(value)
            for name, value in attrs
            if value is not None
        }
        for attribute in ("href", "src"):
            value = attributes.get(attribute)
            if value:
                self.links.append(value)
        if tag.casefold() == "form":
            self.forms.append(
                {
                    "action": attributes.get("action", ""),
                    "method": attributes.get("method", "get").upper(),
                }
            )
        for name, value in attributes.items():
            if name.startswith("data-"):
                self.data_attributes.append((tag.casefold(), name, value))


def validate_transfernow_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        raise ValueError("URL TransferNow inválida.") from None
    if parsed.scheme.casefold() != "https" or not (
        hostname == "transfernow.net" or hostname.endswith(".transfernow.net")
    ):
        raise ValueError("A URL deve pertencer ao domínio oficial do TransferNow.")
    if not parsed.path.casefold().startswith("/dl/"):
        raise ValueError("A URL deve usar o caminho público /dl/ do TransferNow.")
    return url.strip()


def read_html_preview(response: requests.Response) -> bytes:
    content = bytearray()
    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        remaining = MAX_HTML_BYTES - len(content)
        content.extend(chunk[:remaining])
        if len(content) >= MAX_HTML_BYTES:
            break
    return bytes(content)


def unique(values):
    return list(dict.fromkeys(value for value in values if value))


def inspect(url: str) -> int:
    validated_url = validate_transfernow_url(url)
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    try:
        response = session.get(
            validated_url,
            allow_redirects=True,
            stream=True,
            timeout=(15, 30),
            headers={"User-Agent": USER_AGENT},
        )
    except requests.TooManyRedirects:
        print("Conclusão: Fluxo incompatível com requests")
        return 1
    except requests.RequestException:
        print("Conclusão: Fluxo incompatível com requests")
        return 1

    try:
        content_type = response.headers.get("Content-Type", "não informado")
        content_disposition = response.headers.get(
            "Content-Disposition", "não informado"
        )
        print(f"Código HTTP: {response.status_code}")
        print(f"URL final após redirects: {response.url}")
        print(f"Quantidade de redirects: {len(response.history)}")
        print(f"Content-Type: {content_type}")
        print(f"Content-Disposition: {content_disposition}")
        print(f"Tamanho informado: {response.headers.get('Content-Length', 'não informado')}")
        print(f"Server: {response.headers.get('Server', 'não informado')}")

        normalized_content_type = content_type.split(";", 1)[0].strip().casefold()
        is_html = normalized_content_type in {"text/html", "application/xhtml+xml"}
        if not is_html and 200 <= response.status_code < 300:
            print("Conclusão: Download direto disponível")
            return 0
        if not is_html:
            print("Conclusão: Fluxo incompatível com requests")
            return 1

        preview = read_html_preview(response)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="transfernow_inspection_",
            suffix=".html",
            delete=False,
        ) as preview_file:
            preview_file.write(preview)
            preview_path = preview_file.name
        encoding = response.encoding or "utf-8"
        html = preview.decode(encoding, errors="replace")
        parser = InspectionParser()
        parser.feed(html)
        parser.close()

        links = unique(urljoin(response.url, value) for value in parser.links)
        api_endpoints = unique(
            urljoin(response.url, match.group("endpoint"))
            for match in API_PATTERN.finditer(html)
        )
        javascript_urls = unique(
            urljoin(response.url, match.group("url"))
            for match in JS_URL_PATTERN.finditer(html)
        )

        print(f"Amostra HTML salva: {preview_path}")
        print(f"Bytes HTML salvos: {len(preview)}")
        print("Links: " + json.dumps(links, ensure_ascii=False))
        print("Formulários: " + json.dumps(parser.forms, ensure_ascii=False))
        print("Endpoints /api/: " + json.dumps(api_endpoints, ensure_ascii=False))
        print(
            "Atributos data-*: "
            + json.dumps(parser.data_attributes, ensure_ascii=False)
        )
        print(
            "Variáveis JavaScript com URLs: "
            + json.dumps(javascript_urls, ensure_ascii=False)
        )

        identifiable = bool(api_endpoints or javascript_urls or parser.forms)
        if identifiable:
            print("Conclusão: Página HTML com endpoint identificável")
        elif re.search(r"<script\b", html, re.IGNORECASE):
            print("Conclusão: JavaScript obrigatório")
        else:
            print("Conclusão: Fluxo incompatível com requests")
        return 0
    finally:
        response.close()
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspeciona uma URL /dl/ do TransferNow sem baixar o arquivo."
    )
    parser.add_argument("url", help="URL HTTPS oficial do TransferNow")
    arguments = parser.parse_args()
    return inspect(arguments.url)


if __name__ == "__main__":
    raise SystemExit(main())

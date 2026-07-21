"""Inspeciona links do e-mail TransferNow mais recente sem acessá-los."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from integrations.gmail_connector import GmailConnector


TRANSFERNOW_QUERY = "{from:(transfernow.net) subject:(TransferNow)}"
URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
FILE_EXTENSIONS = {
    ".7z",
    ".dcm",
    ".dicom",
    ".gz",
    ".pdf",
    ".rar",
    ".tar",
    ".zip",
}
IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


@dataclass(frozen=True)
class FoundLink:
    url: str
    source: str
    tag: str | None = None
    text: str = ""


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[FoundLink] = []
        self._anchor_url: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name.casefold(): value for name, value in attrs if value}
        normalized_tag = tag.casefold()
        if normalized_tag == "a" and attributes.get("href"):
            self._anchor_url = attributes["href"].strip()
            self._anchor_text = []
        elif normalized_tag in {"img", "source"} and attributes.get("src"):
            self.links.append(
                FoundLink(attributes["src"].strip(), "HTML", normalized_tag)
            )

    def handle_data(self, data: str) -> None:
        if self._anchor_url is not None and data:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._anchor_url is None:
            return
        text = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
        self.links.append(FoundLink(self._anchor_url, "HTML", "a", text))
        self._anchor_url = None
        self._anchor_text = []


def extract_html_links(body: str) -> list[FoundLink]:
    parser = LinkParser()
    parser.feed(body or "")
    parser.close()
    return parser.links


def extract_text_links(body: str) -> list[FoundLink]:
    return [
        FoundLink(match.group(0).rstrip(".,);]}>"), "texto simples")
        for match in URL_PATTERN.finditer(body or "")
    ]


def official_transfernow_domain(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return hostname == "transfernow.net" or hostname.endswith(".transfernow.net")


def classify(link: FoundLink) -> str:
    try:
        parsed = urlsplit(link.url)
        path = parsed.path.casefold()
    except ValueError:
        return "outros"
    text = link.text.casefold()
    suffix = next((extension for extension in IMAGE_EXTENSIONS if path.endswith(extension)), None)
    if link.tag in {"img", "source"} or suffix is not None:
        return "imagens"
    if any(fragment in path or fragment in text for fragment in ("unsubscribe", "descadastrar")):
        return "unsubscribe"
    if (
        path.startswith("/dl/")
        or "download" in path
        or any(word in text for word in ("download", "baixar", "obter arquivos"))
    ):
        return "download"
    return "outros"


def likely_download_index(links: list[FoundLink]) -> int | None:
    scored: list[tuple[int, int]] = []
    for index, link in enumerate(links, start=1):
        if classify(link) != "download":
            continue
        try:
            path = urlsplit(link.url).path.casefold()
        except ValueError:
            path = ""
        score = 100 if official_transfernow_domain(link.url) else 0
        score += 50 if path.startswith("/dl/") else 0
        score += 20 if link.source == "HTML" else 0
        score += 10 if "download" in link.text.casefold() else 0
        scored.append((score, index))
    return max(scored, default=(0, None))[1]


def direct_file_link(url: str) -> bool:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        return False
    return any(path.endswith(extension) for extension in FILE_EXTENSIONS)


def main() -> int:
    messages = GmailConnector().list_messages(
        query=TRANSFERNOW_QUERY,
        max_results=5,
    )
    if not messages:
        raise RuntimeError("Nenhum e-mail do TransferNow foi encontrado.")
    message = max(messages, key=lambda item: item.received_at)
    links = extract_html_links(message.html_body) + extract_text_links(message.text_body)
    candidate_index = likely_download_index(links)

    print(f"Assunto: {message.subject}")
    print(f"Remetente: {message.sender}")
    print(f"Data: {message.received_at.isoformat()}")
    print()
    print("Links encontrados:")
    for index, link in enumerate(links, start=1):
        print(f"[{index}]")
        print(f"Origem: {link.source}")
        print(f"Classificação: {classify(link)}")
        print(f"URL: {link.url}")

    print()
    if candidate_index is None:
        print("Link de download provável: não identificado")
        print("Link direto para arquivo: indeterminado")
        print("Página intermediária: indeterminado")
        print("Parece depender de JavaScript: indeterminado")
        print("Domínio oficial do TransferNow: indeterminado")
        return 0

    candidate = links[candidate_index - 1]
    is_direct = direct_file_link(candidate.url)
    print(f"Link de download provável: [{candidate_index}]")
    print(f"Link direto para arquivo: {'sim' if is_direct else 'não'}")
    print(f"Página intermediária: {'não' if is_direct else 'sim'}")
    print(
        "Parece depender de JavaScript: "
        f"{'não' if is_direct else 'indeterminado sem acessar a página'}"
    )
    print(
        "Domínio oficial do TransferNow: "
        f"{'sim' if official_transfernow_domain(candidate.url) else 'não'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

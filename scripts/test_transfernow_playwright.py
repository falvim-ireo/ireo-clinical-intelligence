"""Diagnóstico manual do clique "Download file" no TransferNow."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


DOWNLOAD_LABEL = "Download file"
DOWNLOAD_EQUIVALENT = re.compile(
    r"download|continue|start|save|open",
    re.IGNORECASE,
)
CONSENT_TEXT = re.compile(
    r"^(?:accept|accept all|agree|allow all|continue|i understand|"
    r"accept cookies|allow cookies|cookies)$",
    re.IGNORECASE,
)
PROTECTED_TEXT = re.compile(
    r"captcha|enter (?:the )?password|digite (?:a )?senha|"
    r"authentication required|login required|sign in to download",
    re.IGNORECASE,
)
NAVIGATION_TIMEOUT_MS = 45_000
BLOCKED_DOMAINS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "dreamstime.com",
)


def validate_url(url: str) -> str:
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
        raise ValueError("A URL deve usar o caminho /dl/ do TransferNow.")
    return url.strip()


def safe_url(url: str) -> str:
    """Mantém o endpoint reconhecível sem imprimir chaves completas."""

    try:
        parsed = urlsplit(url)
    except ValueError:
        return "URL inválida"
    parts = []
    for part in parsed.path.split("/"):
        if len(part) > 16:
            parts.append(f"{part[:5]}...[mascarado]")
        else:
            parts.append(part)
    query_keys = [item.split("=", 1)[0] for item in parsed.query.split("&") if item]
    safe_query = "&".join(f"{key}=[mascarado]" for key in query_keys)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/".join(parts), safe_query, "")
    )


def sanitize_html(value: str, limit: int = 600) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    compact = re.sub(
        r'(?P<prefix>\b(?:href|src|action)=["\'])(?P<url>https?://[^"\']+)',
        lambda match: match.group("prefix") + safe_url(match.group("url")),
        compact,
        flags=re.IGNORECASE,
    )
    compact = re.sub(
        r"(?<![A-Za-z])[A-Za-z0-9_-]{24,}(?![A-Za-z])",
        "[mascarado]",
        compact,
    )
    return compact[:limit]


def first_visible(locator: Locator, limit: int = 30) -> Locator | None:
    for index in range(min(locator.count(), limit)):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


def exact_download_element(page: Page) -> Locator | None:
    return first_visible(page.get_by_text(DOWNLOAD_LABEL, exact=True))


def element_role(element: Locator, tag: str) -> str:
    explicit = element.get_attribute("role")
    if explicit:
        return explicit
    if tag == "button":
        return "button (implícito)"
    if tag == "a" and element.get_attribute("href"):
        return "link (implícito)"
    return "não informado"


def inspect_element(element: Locator | None, label: str) -> None:
    print(f"Inspeção do elemento Download file ({label}):")
    if element is None:
        print("Elemento encontrado: não")
        return
    try:
        tag = element.evaluate("element => element.tagName.toLowerCase()")
        visible = element.is_visible()
        enabled = element.is_enabled()
        box = element.bounding_box()
        outer_html = element.evaluate("element => element.outerHTML")
        print("Elemento encontrado: sim")
        print(f"Tag HTML: {tag}")
        print(f"Role: {element_role(element, tag)}")
        print(f"Visible: {'sim' if visible else 'não'}")
        print(f"Enabled: {'sim' if enabled else 'não'}")
        print(f"Bounding box: {box}")
        for attribute in ("href", "type", "disabled", "aria-disabled"):
            value = element.get_attribute(attribute)
            if attribute == "href" and value:
                value = safe_url(value)
            print(f"Atributo {attribute}: {value or 'não informado'}")
        print(f"outerHTML: {sanitize_html(outer_html)}")
    except PlaywrightError:
        print("Elemento encontrado, mas mudou durante a inspeção.")


def list_frames(page: Page) -> None:
    print("Frames/iframes encontrados:")
    for index, frame in enumerate(page.frames, start=1):
        print(f"[{index}] {safe_url(frame.url)}")


def consent_buttons(page: Page) -> list[tuple[str, Locator]]:
    found: list[tuple[str, Locator]] = []
    buttons = page.get_by_role("button")
    for index in range(min(buttons.count(), 50)):
        candidate = buttons.nth(index)
        try:
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
            accessible_name = candidate.get_attribute("aria-label") or ""
            name = text or accessible_name.strip()
            if name and CONSENT_TEXT.fullmatch(name):
                found.append((name, candidate))
        except PlaywrightError:
            continue
    return found


def dismiss_unambiguous_consent(page: Page) -> bool:
    candidates = consent_buttons(page)
    print("Controles de consentimento visíveis:")
    for name, _ in candidates:
        print(f"- {name}")
    if not candidates:
        return False
    priorities = ("accept all", "allow all", "accept cookies", "allow cookies")
    selected = next(
        (
            candidate
            for name, candidate in candidates
            if name.casefold() in priorities
        ),
        candidates[0] if len(candidates) == 1 else None,
    )
    if selected is None:
        return False
    selected.click(timeout=5_000)
    page.wait_for_timeout(1_000)
    print("Consentimento inequívoco removido: sim")
    return True


def protected_interaction_visible(page: Page) -> bool:
    try:
        protected = page.locator(
            'iframe[src*="captcha" i], iframe[title*="captcha" i], '
            'input[type="password"]'
        )
        for index in range(protected.count()):
            if protected.nth(index).is_visible():
                return True
        body_text = page.locator("body").inner_text(timeout=5_000)
        return PROTECTED_TEXT.search(body_text) is not None
    except PlaywrightError:
        return False


def print_buttons(page: Page) -> None:
    print("Botões após o clique:")
    buttons = page.get_by_role("button")
    for index in range(min(buttons.count(), 100)):
        button = buttons.nth(index)
        try:
            text = re.sub(r"\s+", " ", button.inner_text()).strip()
            aria = button.get_attribute("aria-label") or ""
            label = text or aria or "sem texto"
            print(
                f"[{index + 1}] texto={label!r}; "
                f"visible={button.is_visible()}; enabled={button.is_enabled()}"
            )
        except PlaywrightError:
            print(f"[{index + 1}] indisponível durante a inspeção")


def print_links(page: Page) -> None:
    print("Links após o clique:")
    links = page.locator("a")
    for index in range(min(links.count(), 200)):
        link = links.nth(index)
        try:
            text = re.sub(r"\s+", " ", link.inner_text()).strip() or "sem texto"
            href = link.get_attribute("href") or "sem href"
            print(
                f"[{index + 1}] texto={text!r}; href={safe_url(href)}; "
                f"visible={link.is_visible()}"
            )
        except PlaywrightError:
            print(f"[{index + 1}] indisponível durante a inspeção")


def print_forms(page: Page) -> None:
    print("Formulários após o clique:")
    forms = page.locator("form")
    for index in range(min(forms.count(), 50)):
        form = forms.nth(index)
        try:
            action = form.get_attribute("action") or "não informado"
            method = form.get_attribute("method") or "GET"
            print(
                f"[{index + 1}] action={safe_url(action)}; method={method.upper()}; "
                f"visible={form.is_visible()}"
            )
        except PlaywrightError:
            print(f"[{index + 1}] indisponível durante a inspeção")


def second_step_controls(page: Page, *, print_matches: bool) -> list[str]:
    if print_matches:
        print("Elementos contendo Download, Continue, Start, Save ou Open:")
    candidates = page.locator("button, a, [role='button'], input[type='submit']")
    matches: list[str] = []
    for index in range(min(candidates.count(), 200)):
        candidate = candidates.nth(index)
        try:
            text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
            if not text:
                text = (
                    candidate.get_attribute("value")
                    or candidate.get_attribute("aria-label")
                    or ""
                ).strip()
            if not text or DOWNLOAD_EQUIVALENT.search(text) is None:
                continue
            tag = candidate.evaluate("element => element.tagName.toLowerCase()")
            href = candidate.get_attribute("href") or ""
            signature = f"{tag}|{text.casefold()}|{href}"
            if signature not in matches:
                matches.append(signature)
                if print_matches:
                    print(
                        f"- texto={text!r}; tag={tag}; "
                        f"visible={candidate.is_visible()}; enabled={candidate.is_enabled()}"
                    )
        except PlaywrightError:
            continue
    return matches


def inspect_and_download(url: str) -> int:
    validated_url = validate_url(url)
    conclusion = "Elemento de download bloqueado ou não clicável"
    browser = None
    try:
        with sync_playwright() as playwright:
            downloads_path = Path(
                tempfile.mkdtemp(prefix="ireo_transfernow_downloads_")
            )
            browser = playwright.chromium.launch(
                headless=False,
                downloads_path=str(downloads_path),
            )
            context = browser.new_context(accept_downloads=True)

            def block_ads_and_external_navigation(route) -> None:
                request = route.request
                try:
                    hostname = (
                        urlsplit(request.url).hostname or ""
                    ).casefold().rstrip(".")
                except ValueError:
                    route.abort()
                    return
                blocked_ad = any(
                    hostname == domain or hostname.endswith(f".{domain}")
                    for domain in BLOCKED_DOMAINS
                )
                official_transfernow = (
                    hostname == "transfernow.net"
                    or hostname.endswith(".transfernow.net")
                )
                external_document = (
                    request.resource_type == "document" and not official_transfernow
                )
                if blocked_ad or external_document:
                    print(f"Requisição bloqueada: {safe_url(request.url)}")
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", block_ads_and_external_navigation)
            page = context.new_page()
            page.set_default_timeout(10_000)
            page.goto(
                validated_url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(2_000)

            diagnostic_dir = Path(
                tempfile.mkdtemp(prefix="ireo_transfernow_diagnostic_")
            )
            before_path = diagnostic_dir / "before_interaction.png"
            after_path = diagnostic_dir / "after_attempt.png"
            page.screenshot(path=before_path, full_page=True)
            print(f"Screenshot antes da interação: {before_path}")
            print(f"Título da página: {page.title()}")
            print(f"URL final: {safe_url(page.url)}")
            list_frames(page)

            element = exact_download_element(page)
            inspect_element(element, "antes do consentimento")
            dismiss_unambiguous_consent(page)
            element = exact_download_element(page)
            inspect_element(element, "após o consentimento")

            if protected_interaction_visible(page):
                conclusion = "Fluxo bloqueado por interação protegida"
            elif element is None:
                conclusion = "Elemento de download bloqueado ou não clicável"
            else:
                try:
                    if not element.is_visible() or not element.is_enabled():
                        conclusion = (
                            "Elemento de download bloqueado ou não clicável"
                        )
                    else:
                        try:
                            print("Capturando download com page.expect_download().")
                            with page.expect_download(timeout=60_000) as download_info:
                                element.scroll_into_view_if_needed(timeout=10_000)
                                element.click(timeout=30_000)
                            download = download_info.value
                        except PlaywrightTimeoutError:
                            conclusion = "Clique executado, mas download não emitido"
                        else:
                            suggested_filename = Path(
                                download.suggested_filename
                            ).name
                            download_path = download.path()
                            failure = download.failure()
                            exists = bool(download_path and Path(download_path).exists())
                            size = (
                                Path(download_path).stat().st_size
                                if download_path and exists
                                else 0
                            )
                            print(f"suggested_filename: {suggested_filename}")
                            print(f"path(): {download_path}")
                            print(f"failure(): {failure}")
                            print(f"exists(path): {exists}")
                            print(f"size(bytes): {size}")
                            if download_path and exists:
                                destination_dir = (
                                    Path(__file__).resolve().parents[1] / "tmp"
                                )
                                destination_dir.mkdir(parents=True, exist_ok=True)
                                destination = destination_dir / suggested_filename
                                shutil.copy2(download_path, destination)
                                print(f"Arquivo copiado para: {destination}")
                            conclusion = "Download capturado com Playwright"
                except PlaywrightError:
                    conclusion = (
                        "Elemento de download bloqueado ou não clicável"
                    )

            try:
                page.screenshot(path=after_path, full_page=True)
                print(f"Screenshot depois da tentativa: {after_path}")
            except PlaywrightError:
                print("Screenshot depois da tentativa: não disponível")
            return 0 if conclusion == "Download capturado com Playwright" else 1
    except PlaywrightTimeoutError:
        conclusion = "Elemento de download bloqueado ou não clicável"
        return 1
    except (PlaywrightError, OSError):
        conclusion = "Fluxo bloqueado por interação protegida"
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except PlaywrightError:
                pass
        print(f"Conclusão: {conclusion}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Diagnostica o controle exato "Download file" no TransferNow.'
    )
    parser.add_argument("url", help="URL HTTPS /dl/ oficial do TransferNow")
    arguments = parser.parse_args()
    try:
        return inspect_and_download(arguments.url)
    except ValueError:
        print("Conclusão: Fluxo bloqueado por interação protegida")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

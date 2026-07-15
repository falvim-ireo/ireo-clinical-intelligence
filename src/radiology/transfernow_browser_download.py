"""Download supervisionado em Chromium para landing pages do TransferNow."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
import re
from typing import Callable

from radiology.transfernow_download import (
    DownloadResult,
    TransferNowDownloader,
    TransferNowDownloadError,
)


class TransferNowBrowserDownloader:
    """Captura exatamente um download em um perfil temporário do Chromium."""

    DOWNLOAD_LABELS = ("Download", "Baixar")
    MAX_CONTROL_CANDIDATES = 20

    def __init__(
        self,
        *,
        timeout_seconds: int = 3600,
        max_download_bytes: int = 10_737_418_240,
        headless: bool = False,
        playwright_factory=None,
        output: Callable[[str], None] = print,
        debug: bool = False,
        allow_manual_interaction: bool = True,
    ) -> None:
        self.timeout_ms = timeout_seconds * 1000
        self.max_download_bytes = max_download_bytes
        self.headless = headless
        self.playwright_factory = playwright_factory
        self.output = output
        self.debug = debug
        self.allow_manual_interaction = allow_manual_interaction

    def download(
        self,
        url: str,
        original_filename: str | None,
        quarantine_root: str | Path,
        correlation_id: str,
        message_id: str,
    ) -> DownloadResult:
        del original_filename, message_id  # suggested_filename é a fonte real no navegador.
        TransferNowDownloader._validate_url(url)
        folder = self._folder(quarantine_root, correlation_id)
        factory = self.playwright_factory or self._sync_playwright
        expected_types = None
        if self.playwright_factory is None:
            from playwright.sync_api import Browser, BrowserContext, Page

            expected_types = (Browser, BrowserContext, Page)
        stage = "PLAYWRIGHT_START"
        browser = None
        context = None
        page = None
        try:
            with factory() as playwright:
                stage = "BROWSER_LAUNCH"
                executable_path = Path(playwright.chromium.executable_path)
                if not executable_path.is_file():
                    raise FileNotFoundError("Chromium executable not found")
                browser = playwright.chromium.launch(headless=self.headless)
                self._require_sync_object(
                    browser, "browser", "new_context",
                    expected_types[0] if expected_types else None,
                )
                try:
                    stage = "CONTEXT_CREATE"
                    context = browser.new_context(accept_downloads=True)
                    self._require_sync_object(
                        context, "context", "new_page",
                        expected_types[1] if expected_types else None,
                    )
                    stage = "PAGE_CREATE"
                    page = context.new_page()
                    self._require_sync_object(
                        page, "page", "goto",
                        expected_types[2] if expected_types else None,
                    )
                    downloads = []

                    def record_download(download) -> None:
                        downloads.append(download)

                    page.on("download", record_download)
                    page.on("popup", lambda popup: popup.close())
                    stage = "NAVIGATION"
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    stage = "DOMAIN_VALIDATION"
                    TransferNowDownloader._validate_url(page.url)
                    stage = "PROTECTION_CHECK"
                    if self._has_access_protection(page):
                        raise TransferNowDownloadError(
                            "A página exige autenticação ou verificação humana."
                        )
                    stage = "DOWNLOAD_CONTROL_SEARCH"
                    control = self._download_control(page)
                    stage = "DOWNLOAD_WAIT"
                    captured = self._capture_download(page, control)
                    if len(downloads) > 1:
                        captured.cancel()
                        raise TransferNowDownloadError(
                            "Mais de um download foi iniciado; operação abortada."
                        )
                    stage = "DOWNLOAD_SAVE"
                    return self._save(captured, folder)
                finally:
                    stage_before_close = stage
                    stage = "BROWSER_CLOSE"
                    try:
                        try:
                            if callable(getattr(page, "close", None)):
                                page.close()
                        finally:
                            if callable(getattr(context, "close", None)):
                                context.close()
                    finally:
                        browser.close()
                    stage = stage_before_close
        except Exception as exc:
            raise self._diagnostic_error(stage, exc) from None

    def _capture_download(self, page, control):
        if control is not None:
            with page.expect_download(timeout=self.timeout_ms) as info:
                control.click()
            return info.value
        if not self.allow_manual_interaction:
            raise TransferNowDownloadError(
                "O controle de download não foi localizado automaticamente."
            )
        self.output("Clique manualmente no botão de download da página.")
        return page.wait_for_event("download", timeout=self.timeout_ms)

    def _has_access_protection(self, page) -> bool:
        if re.search(r"/(?:login|signin|auth)(?:[/?#]|$)", page.url, re.IGNORECASE):
            return True
        protection_pattern = re.compile(
            r"captcha|recaptcha|hcaptcha|faça login|iniciar sessão|"
            r"digite (?:a )?senha|enter (?:the )?password",
            re.IGNORECASE,
        )
        try:
            locator = page.get_by_text(protection_pattern)
            if self._first_usable(locator) is not None:
                return True
            selectors = (
                'input[type="password"]',
                'iframe[src*="captcha" i], iframe[title*="captcha" i], '
                '[class*="captcha" i], [id*="captcha" i]',
            )
            return any(
                self._first_usable(page.locator(selector)) is not None
                for selector in selectors
            )
        except (AttributeError, TypeError):
            return False

    def _download_control(self, page):
        label_pattern = re.compile(r"download|baixar", re.IGNORECASE)
        button_locator = page.get_by_role("button", name=label_pattern)
        candidate = self._first_usable(button_locator)
        if candidate is not None:
            return candidate

        link_locator = page.get_by_role("link", name=label_pattern)
        candidate = self._first_usable(
            link_locator, page=page, require_transfernow_href=True
        )
        if candidate is not None:
            return candidate

        text_locator = page.get_by_text(label_pattern)
        candidate = self._first_usable(text_locator)
        if candidate is not None:
            return candidate

        href_locator = page.locator("a[href]")
        candidate = self._first_usable(
            href_locator, page=page, require_transfernow_href=True
        )
        if candidate is not None:
            return candidate
        return None

    def _first_usable(self, locator, *, page=None, require_transfernow_href=False):
        from urllib.parse import urljoin

        if not self._is_locator_candidate(locator):
            return None
        try:
            count = min(locator.count(), self.MAX_CONTROL_CANDIDATES)
        except (AttributeError, TypeError):
            return None
        for index in range(count):
            try:
                candidate = locator.nth(index)
            except (AttributeError, TypeError):
                continue
            if not self._is_locator_candidate(candidate):
                continue
            try:
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                if require_transfernow_href:
                    href = candidate.get_attribute("href")
                    if not href:
                        continue
                    TransferNowDownloader._validate_url(urljoin(page.url, href))
            except (AttributeError, TypeError, TransferNowDownloadError):
                continue
            return candidate
        return None

    @staticmethod
    def _is_locator_candidate(value) -> bool:
        if value is None or callable(value) or inspect.isawaitable(value):
            return False
        required_methods = (
            "count", "nth", "is_visible", "is_enabled", "get_attribute"
        )
        return all(callable(getattr(value, method, None)) for method in required_methods)

    def _save(self, download, folder: Path) -> DownloadResult:
        TransferNowDownloader._validate_url(download.url)
        filename = TransferNowDownloader._safe_filename(download.suggested_filename)
        final_path = folder / filename
        part_path = folder / f"{filename}.part"
        if final_path.exists() or part_path.exists():
            download.cancel()
            raise TransferNowDownloadError(
                "Já existe arquivo relacionado; revisão humana obrigatória."
            )
        try:
            download.save_as(part_path)
            size = part_path.stat().st_size
            if size == 0:
                raise TransferNowDownloadError("O download retornou um arquivo vazio.")
            if size > self.max_download_bytes:
                raise TransferNowDownloadError("O arquivo excede o limite configurado.")
            digest = hashlib.sha256()
            with part_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            part_path.replace(final_path)
            return DownloadResult(final_path, size, digest.hexdigest())
        except TransferNowDownloadError:
            raise
        except OSError:
            raise TransferNowDownloadError("Não foi possível salvar o download.") from None

    @staticmethod
    def _folder(root, correlation_id: str) -> Path:
        import re

        if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", correlation_id):
            raise TransferNowDownloadError("Correlation ID inválido.")
        quarantine = Path(root).expanduser().resolve()
        folder = quarantine / correlation_id
        if not folder.resolve().is_relative_to(quarantine):
            raise TransferNowDownloadError("Destino de quarentena inválido.")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _sync_playwright():
        from playwright.sync_api import sync_playwright

        return sync_playwright()

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        return exc.__class__.__name__ == "TimeoutError"

    def _diagnostic_error(self, stage: str, exc: Exception) -> TransferNowDownloadError:
        details = {
            "PLAYWRIGHT_START": "Não foi possível iniciar o Playwright.",
            "BROWSER_LAUNCH": "Não foi possível localizar ou iniciar o Chromium.",
            "CONTEXT_CREATE": "Não foi possível criar o contexto temporário.",
            "PAGE_CREATE": "Não foi possível criar a página isolada.",
            "NAVIGATION": "Não foi possível carregar a página permitida.",
            "DOMAIN_VALIDATION": "O domínio final não pôde ser validado.",
            "PROTECTION_CHECK": "A página exige autenticação ou verificação humana.",
            "DOWNLOAD_CONTROL_SEARCH": "Não foi possível examinar os controles de download.",
            "DOWNLOAD_WAIT": "Não foi possível capturar o download no tempo permitido.",
            "DOWNLOAD_SAVE": "Não foi possível salvar o download na quarentena.",
            "BROWSER_CLOSE": "Não foi possível fechar completamente o navegador.",
        }
        detail = details.get(stage, "Falha controlada no navegador.")
        if self._is_timeout(exc):
            detail = "Tempo esgotado durante esta etapa; use --archive-path."
        elif isinstance(exc, TransferNowDownloadError):
            detail = self._sanitize_detail(str(exc)) or detail
        if self.debug:
            attribute_detail = self._attribute_detail(exc)
            technical = attribute_detail or self._sanitize_detail(str(exc))
            if attribute_detail:
                detail = attribute_detail
            elif technical:
                detail = f"{detail} Diagnóstico: {technical}"
        return TransferNowDownloadError(
            "Falha no navegador supervisionado.\n"
            f"Etapa: {stage}\n"
            f"Tipo: {type(exc).__name__}\n"
            f"Detalhe sanitizado: {detail}"
        )

    @staticmethod
    def _require_sync_object(
        value, object_name: str, required_attribute: str, expected_type=None
    ) -> None:
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError(f"{object_name} returned an asynchronous object")
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"{object_name} is {type(value).__name__}, expected {expected_type.__name__}"
            )
        if not hasattr(value, required_attribute):
            type_name = type(value).__name__
            raise AttributeError(
                f"'{type_name}' object has no attribute '{required_attribute}'"
            )

    @staticmethod
    def _attribute_detail(exc: Exception) -> str | None:
        if not isinstance(exc, AttributeError):
            return None
        match = re.search(
            r"'(?P<object>[^']+)' object has no attribute '(?P<attribute>[^']+)'",
            str(exc),
        )
        if not match:
            return "Objeto: desconhecido; atributo ausente: desconhecido."
        return (
            f"Objeto: {match.group('object')}; "
            f"atributo ausente: {match.group('attribute')}."
        )

    @staticmethod
    def _sanitize_detail(value: str) -> str:
        sanitized = re.sub(r"https?://\S+", "[URL OCULTA]", value, flags=re.IGNORECASE)
        sanitized = re.sub(
            r"(?i)\b(token|cookie|authorization|headers?)\b\s*[:=]\s*\S+",
            r"\1=[OCULTO]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)\b[^\s\\/]+\.(rar|zip)\b", r"[NOME OCULTO].\1", sanitized
        )
        sanitized = re.sub(r"[A-Za-z]:\\[^\r\n]+", "[CAMINHO OCULTO]", sanitized)
        return sanitized.strip()[:500]


def run_browser_self_test(
    *, playwright_factory=None, output: Callable[[str], None] = print
) -> None:
    """Inicia Chromium isolado e navega exclusivamente para about:blank."""

    factory = playwright_factory or TransferNowBrowserDownloader._sync_playwright
    browser = None
    context = None
    with factory() as playwright:
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            raise TransferNowDownloadError("Chromium do Playwright não está instalado.")
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto("about:blank")
            output("Browser self-test OK")
            page.wait_for_timeout(3000)
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()

"""Testes offline do fallback Playwright, sem abrir navegador real."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from models.email_message import EmailMessage
from radiology.gmail_import import run_gmail_import
from radiology.transfernow_browser_download import (
    TransferNowBrowserDownloader,
    run_browser_self_test,
)
from radiology.transfernow_download import (
    BrowserInteractionRequired,
    DownloadResult,
    TransferNowDownloadError,
)


class FakeDownload:
    def __init__(self, name="PACIENTE.rar", content=b"rar", url="https://files.transfernow.net/file"):
        self.suggested_filename = name
        self.content = content
        self.url = url
        self.cancelled = False

    def save_as(self, path):
        Path(path).write_bytes(self.content)

    def cancel(self):
        self.cancelled = True


class FakeLocator:
    def __init__(self, page, found=True, href=None, visible=True, enabled=True):
        self.page = page
        self.found = found
        self.href = href
        self.visible = visible
        self.enabled = enabled
        self.count_called = 0
        self.nth_calls = []

    def count(self): self.count_called += 1; return int(self.found)
    def nth(self, index): self.nth_calls.append(index); return self
    def is_visible(self): return self.found and self.visible
    def is_enabled(self): return self.enabled
    def get_attribute(self, name): return self.href if name == "href" else None
    def click(self): self.page.emit_downloads()


class FakeExpectation:
    def __init__(self, page): self.page, self.value = page, None
    def __enter__(self): self.page.expectation = self; return self
    def __exit__(self, *args): self.value = self.page.download


class FakePage:
    def __init__(self, download, *, final_url="https://transfernow.net/dl/x", control=True, control_kind="button", control_visible=True, control_enabled=True, control_href="https://transfernow.net/download/file", multiple=False, timeout=False, navigation_error=None, protection=False):
        self.download = download
        self.url = final_url
        self.control = control
        self.control_kind = control_kind
        self.control_visible = control_visible
        self.control_enabled = control_enabled
        self.control_href = control_href
        self.multiple = multiple
        self.timeout = timeout
        self.navigation_error = navigation_error
        self.protection = protection
        self.handlers = {}
        self.expectation = None
        self.goto_urls = []
        self.closed = False
        self.locators = []

    def on(self, event, callback): self.handlers[event] = callback
    def goto(self, *args, **kwargs):
        self.goto_urls.append(args[0])
        if self.navigation_error: raise self.navigation_error
        if self.timeout: raise TimeoutError()
    def wait_for_timeout(self, milliseconds): assert milliseconds == 3000
    def close(self): self.closed = True
    def _locator(self, found=False, href=None):
        locator = FakeLocator(
            self, found, href, self.control_visible, self.control_enabled
        )
        self.locators.append(locator)
        return locator
    def get_by_role(self, role, **kwargs):
        return self._locator(
            self.control and role == self.control_kind,
            self.control_href if role == "link" else None,
        )
    def get_by_text(self, pattern):
        protection_search = "captcha" in getattr(pattern, "pattern", "").casefold()
        if protection_search and not self.protection:
            return FakeLocator(self, found=False)
        return self._locator(
            (self.protection and protection_search)
            or (self.control and self.control_kind == "text" and not protection_search)
        )
    def locator(self, selector):
        if selector != "a[href]":
            if not self.protection:
                return FakeLocator(self, found=False)
            return self._locator(True)
        return self._locator(
            self.control and self.control_kind == "href", self.control_href
        )
    def expect_download(self, **kwargs): return FakeExpectation(self)
    def emit_downloads(self):
        self.handlers["download"](self.download)
        if self.multiple: self.handlers["download"](FakeDownload("OUTRO.zip"))
    def wait_for_event(self, event, **kwargs):
        if self.timeout: raise TimeoutError()
        self.emit_downloads()
        return self.download


class FakeContext:
    def __init__(self, page): self.page, self.closed, self.options = page, False, None
    def new_page(self): return self.page
    def on(self, *args): pass
    def close(self): self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self.page, self.closed, self.context = page, False, None
        self.close_error = None
    def new_context(self, **kwargs):
        self.context = FakeContext(self.page); self.context.options = kwargs; return self.context
    def close(self):
        self.closed = True
        if self.close_error: raise self.close_error


class FakeChromium:
    def __init__(self, page):
        self.page, self.browser, self.headless = page, None, None
        self.executable_path = __file__
        self.launch_error = None
    def launch(self, **kwargs):
        if self.launch_error: raise self.launch_error
        self.headless = kwargs["headless"]; self.browser = FakeBrowser(self.page); return self.browser


class FakePlaywright:
    def __init__(self, page): self.chromium = FakeChromium(page)
    def __enter__(self): return self
    def __exit__(self, *args): pass


def make_downloader(page, outputs=None):
    fake = FakePlaywright(page)
    instance = TransferNowBrowserDownloader(
        playwright_factory=lambda: fake,
        output=(outputs if outputs is not None else []).append,
    )
    return instance, fake


def run_browser(instance, tmp_path):
    return instance.download(
        "https://transfernow.net/dl/token", "EMAIL.rar", tmp_path,
        "correlation-0001", "message-id",
    )


def test_chromium_visible_auto_button_download_and_temporary_context(tmp_path):
    page = FakePage(FakeDownload())
    instance, fake = make_downloader(page)
    result = run_browser(instance, tmp_path)
    assert fake.chromium.headless is False
    assert fake.chromium.browser.context.options == {"accept_downloads": True}
    assert result.path.name == "PACIENTE.rar"
    assert result.sha256 == hashlib.sha256(b"rar").hexdigest()
    assert page.closed
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


def test_manual_assisted_download_when_button_not_found(tmp_path):
    outputs = []
    instance, _ = make_downloader(FakePage(FakeDownload(), control=False), outputs)
    assert run_browser(instance, tmp_path).size_bytes == 3
    assert outputs == ["Clique manualmente no botão de download da página."]


def test_automatic_visible_mode_never_waits_for_manual_input(tmp_path):
    outputs = []
    page = FakePage(FakeDownload(), control=False)
    instance, fake = make_downloader(page, outputs)
    instance.allow_manual_interaction = False
    with pytest.raises(TransferNowDownloadError, match="DOWNLOAD_WAIT"):
        run_browser(instance, tmp_path)
    assert outputs == []
    assert fake.chromium.headless is False
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


@pytest.mark.parametrize(
    "page",
    (
        FakePage(FakeDownload(), protection=True),
        FakePage(FakeDownload(), final_url="https://transfernow.net/login"),
    ),
)
def test_login_or_captcha_is_not_bypassed_and_browser_closes(tmp_path, page):
    instance, fake = make_downloader(page)
    instance.allow_manual_interaction = False
    with pytest.raises(TransferNowDownloadError, match="PROTECTION_CHECK"):
        run_browser(instance, tmp_path)
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


def test_download_button_uses_locator_count_and_nth(tmp_path):
    page = FakePage(FakeDownload(), control_kind="button")
    instance, _ = make_downloader(page)
    assert run_browser(instance, tmp_path).size_bytes == 3
    button_locator = page.locators[0]
    assert button_locator.count_called == 1
    assert button_locator.nth_calls == [0]
    assert not hasattr(button_locator, "first")


def test_transfernow_link_is_found_with_nth(tmp_path):
    page = FakePage(FakeDownload(), control_kind="link")
    instance, _ = make_downloader(page)
    assert run_browser(instance, tmp_path).path.suffix == ".rar"
    link_locator = page.locators[1]
    assert link_locator.count_called == 1
    assert link_locator.nth_calls == [0]


@pytest.mark.parametrize(
    ("visible", "enabled"),
    ((False, True), (True, False)),
)
def test_hidden_or_disabled_control_uses_manual_fallback(
    tmp_path, visible, enabled
):
    outputs = []
    page = FakePage(
        FakeDownload(), control_visible=visible, control_enabled=enabled
    )
    instance, _ = make_downloader(page, outputs)
    assert run_browser(instance, tmp_path).size_bytes == 3
    assert outputs == ["Clique manualmente no botão de download da página."]


def test_visible_text_fallback_is_used_without_locator_iteration(tmp_path):
    page = FakePage(FakeDownload(), control_kind="text")
    instance, _ = make_downloader(page)
    assert run_browser(instance, tmp_path).size_bytes == 3
    text_locator = page.locators[2]
    assert text_locator.count_called == 1
    assert text_locator.nth_calls == [0]


def test_builtin_method_candidate_with_missing_pw_impl_is_ignored():
    instance = TransferNowBrowserDownloader(playwright_factory=lambda: None)
    builtin_method = [].append
    assert type(builtin_method).__name__ == "builtin_function_or_method"
    with pytest.raises(AttributeError, match="_pw_impl_instance_"):
        getattr(builtin_method, "_pw_impl_instance_")
    assert instance._first_usable(builtin_method) is None


def test_locator_nth_method_returned_without_call_is_ignored():
    instance = TransferNowBrowserDownloader(playwright_factory=lambda: None)

    class BrokenLocator:
        def count(self): return 1
        def nth(self, index): return self.nth
        def is_visible(self): return True
        def is_enabled(self): return True
        def get_attribute(self, name): return None

    assert instance._first_usable(BrokenLocator()) is None


def test_page_get_by_role_method_reference_is_not_treated_as_locator():
    instance = TransferNowBrowserDownloader(playwright_factory=lambda: None)
    page = FakePage(FakeDownload(), control=False)
    assert callable(page.get_by_role)
    assert instance._first_usable(page.get_by_role) is None


def test_invalid_method_locators_keep_manual_fallback(tmp_path):
    outputs = []

    class MethodPage(FakePage):
        def get_by_role(self, role, **kwargs): return [].append
        def get_by_text(self, pattern): return self.get_by_text
        def locator(self, selector): return self.locator

    page = MethodPage(FakeDownload())
    instance, _ = make_downloader(page, outputs)
    assert run_browser(instance, tmp_path).size_bytes == 3
    assert outputs == ["Clique manualmente no botão de download da página."]


@pytest.mark.parametrize("final_url", ("https://evil.example/file", "http://transfernow.net/file"))
def test_malicious_page_domain_is_rejected_and_browser_closed(tmp_path, final_url):
    instance, fake = make_downloader(FakePage(FakeDownload(), final_url=final_url))
    with pytest.raises(TransferNowDownloadError, match="Etapa: DOMAIN_VALIDATION"):
        run_browser(instance, tmp_path)
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


@pytest.mark.parametrize("name", ("virus.exe", "../EXAME.rar", "PACIENTE... .rar"))
def test_invalid_suggested_filename_or_extension_is_rejected(tmp_path, name):
    instance, _ = make_downloader(FakePage(FakeDownload(name=name)))
    with pytest.raises(TransferNowDownloadError): run_browser(instance, tmp_path)


def test_empty_file_is_rejected_and_part_is_preserved(tmp_path):
    instance, _ = make_downloader(FakePage(FakeDownload(content=b"")))
    with pytest.raises(TransferNowDownloadError, match="vazio"): run_browser(instance, tmp_path)
    assert list(tmp_path.rglob("*.part"))


def test_timeout_closes_browser_and_offers_archive_fallback(tmp_path):
    instance, fake = make_downloader(FakePage(FakeDownload(), timeout=True))
    with pytest.raises(
        TransferNowDownloadError, match="(?s)Etapa: NAVIGATION.*--archive-path"
    ): run_browser(instance, tmp_path)
    assert fake.chromium.browser.closed


def test_two_downloads_are_aborted(tmp_path):
    download = FakeDownload()
    instance, _ = make_downloader(FakePage(download, multiple=True))
    with pytest.raises(TransferNowDownloadError, match="Mais de um"): run_browser(instance, tmp_path)
    assert download.cancelled


def test_download_from_external_domain_is_rejected(tmp_path):
    download = FakeDownload(url="https://evil.example/archive.rar")
    instance, _ = make_downloader(FakePage(download))
    with pytest.raises(TransferNowDownloadError, match="Etapa: DOWNLOAD_SAVE"):
        run_browser(instance, tmp_path)


def test_playwright_start_failure_reports_stage_and_type_without_secrets(tmp_path):
    secret = "https://transfernow.net/dl/secret-token"

    def fail():
        raise RuntimeError(secret)

    instance = TransferNowBrowserDownloader(playwright_factory=fail, debug=True)
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    message = str(captured.value)
    assert "Etapa: PLAYWRIGHT_START" in message
    assert "Tipo: RuntimeError" in message
    assert secret not in message and "secret-token" not in message


def test_missing_chromium_reports_browser_launch(tmp_path):
    instance, fake = make_downloader(FakePage(FakeDownload()))
    fake.chromium.executable_path = str(tmp_path / "missing" / "chrome.exe")
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    assert "Etapa: BROWSER_LAUNCH" in str(captured.value)
    assert "Tipo: FileNotFoundError" in str(captured.value)


def test_launch_failure_is_sanitized(tmp_path):
    instance, fake = make_downloader(FakePage(FakeDownload()))
    fake.chromium.launch_error = RuntimeError(
        "token=secret https://example.test/private"
    )
    instance.debug = True
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    message = str(captured.value)
    assert "Etapa: BROWSER_LAUNCH" in message
    assert "secret" not in message and "https://" not in message


def test_navigation_failure_reports_stage_and_closes_browser(tmp_path):
    page = FakePage(FakeDownload(), navigation_error=OSError("navigation failed"))
    instance, fake = make_downloader(page)
    with pytest.raises(TransferNowDownloadError, match="Etapa: NAVIGATION"):
        run_browser(instance, tmp_path)
    assert page.closed
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


def test_browser_new_context_returns_context_and_context_new_page_returns_page(tmp_path):
    page = FakePage(FakeDownload())
    instance, fake = make_downloader(page)
    run_browser(instance, tmp_path)
    assert isinstance(fake.chromium.browser.context, FakeContext)
    assert fake.chromium.browser.context.page is page


@pytest.mark.parametrize("invalid_context", (object(), 42))
def test_invalid_context_or_missing_new_page_is_reported_safely(
    tmp_path, invalid_context
):
    instance, fake = make_downloader(FakePage(FakeDownload()))

    def invalid_new_context(**kwargs):
        return invalid_context

    original_launch = fake.chromium.launch

    def launch(**kwargs):
        browser = original_launch(**kwargs)
        browser.new_context = invalid_new_context
        return browser

    fake.chromium.launch = launch
    instance.debug = True
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    message = str(captured.value)
    assert "Etapa: CONTEXT_CREATE" in message
    assert "atributo ausente: new_page" in message
    assert "https://" not in message
    assert fake.chromium.browser.closed


def test_async_context_is_rejected_without_await_or_api_mixing(tmp_path):
    instance, fake = make_downloader(FakePage(FakeDownload()))

    async def async_context():
        return FakeContext(FakePage(FakeDownload()))

    original_launch = fake.chromium.launch

    def launch(**kwargs):
        browser = original_launch(**kwargs)
        browser.new_context = lambda **unused: async_context()
        return browser

    fake.chromium.launch = launch
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    assert "Etapa: CONTEXT_CREATE" in str(captured.value)
    assert "Tipo: TypeError" in str(captured.value)
    assert fake.chromium.browser.closed


def test_browser_close_failure_reports_close_stage(tmp_path):
    page = FakePage(FakeDownload())
    instance, fake = make_downloader(page)
    original_launch = fake.chromium.launch

    def launch(**kwargs):
        browser = original_launch(**kwargs)
        browser.close_error = OSError("close failed")
        return browser

    fake.chromium.launch = launch
    with pytest.raises(TransferNowDownloadError) as captured:
        run_browser(instance, tmp_path)
    assert "Etapa: BROWSER_CLOSE" in str(captured.value)
    assert fake.chromium.browser.closed


def test_browser_self_test_uses_about_blank_and_closes():
    page = FakePage(FakeDownload())
    fake = FakePlaywright(page)
    outputs = []
    run_browser_self_test(playwright_factory=lambda: fake, output=outputs.append)
    assert outputs == ["Browser self-test OK"]
    assert page.goto_urls == ["about:blank"]
    assert fake.chromium.headless is False
    assert fake.chromium.browser.closed and fake.chromium.browser.context.closed


def test_browser_fallback_opens_and_continues_without_confirmation(tmp_path):
    message = EmailMessage(
        message_id="secret", subject='TransferNow "PACIENTE.rar"', sender="TransferNow",
        reply_to=None, received_at=datetime.now(timezone.utc),
        text_body="fixture1@example.com https://transfernow.net/dl/token",
    )
    class Gmail:
        def list_messages(self, **kwargs): return [message]
    class Http:
        def download(self, *args): raise BrowserInteractionRequired("landing")
    calls = []
    class Browser:
        def download(self, *args):
            path = tmp_path / "PACIENTE.rar"
            path.write_bytes(b"rar")
            return DownloadResult(path, 3, hashlib.sha256(b"rar").hexdigest())
    class Importer:
        def run(self, **kwargs):
            calls.append(kwargs)
            return "published"
    outcome = run_gmail_import(
        gmail=Gmail(), downloader=Http(), browser_downloader=Browser(), importer=Importer(),
        quarantine_root=tmp_path, correlation_id="correlation-0001",
        input_func=lambda prompt: pytest.fail(f"prompt inesperado: {prompt}"),
        output=lambda _: None,
    )
    assert outcome.import_result == "published"
    assert calls[0]["archive_path"] == outcome.download.path


@pytest.mark.parametrize(
    "browser_confirmation",
    ("ABRIR NAVEGADOR", "abrir navegador", "Abrir Navegador", "  abrir navegador  "),
)
def test_landing_page_browser_download_reuses_supervised_importer(
    tmp_path, browser_confirmation
):
    message = EmailMessage(
        message_id="secret", subject='TransferNow "PACIENTE.rar"', sender="TransferNow",
        reply_to=None, text_body="fixture1@example.com https://transfernow.net/dl/token",
    )
    class Gmail:
        def list_messages(self, **kwargs): return [message]
    class Http:
        def download(self, *args): raise BrowserInteractionRequired("landing")
    archive = tmp_path / "PACIENTE.rar"
    archive.write_bytes(b"rar")
    class Browser:
        def download(self, *args): return DownloadResult(archive, 3, hashlib.sha256(b"rar").hexdigest())
    imported = []
    class Importer:
        def run(self, *, archive_path): imported.append(archive_path); return "ok"
    answers = iter(("1", "confirmar", browser_confirmation, "confirmar"))
    outcome = run_gmail_import(
        gmail=Gmail(), downloader=Http(), browser_downloader=Browser(), importer=Importer(),
        quarantine_root=tmp_path, correlation_id="correlation-0001",
        input_func=lambda _: next(answers), output=lambda _: None,
    )
    assert outcome.import_result == "ok" and imported == [archive]


@pytest.mark.skip(reason="Opt-in: requer URL real e autorização explícita para abrir Chromium")
def test_real_transfernow_browser_opt_in():
    pass

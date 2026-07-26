from acquisition.cfaz_browser_session import CfazBrowserSession, CfazBrowserSessionError


class Req:
    def __init__(self, url): self.url, self.method = url, "GET"


class Route:
    def __init__(self, req): self.request, self.aborted = req, False
    def abort(self): self.aborted = True
    def continue_(self): pass


class Page:
    def __init__(self, urls): self.urls = urls
    def route(self, _pattern, handler):
        self.handler = handler
    def goto(self, *_args, **_kwargs):
        for url in self.urls:
            self.handler(Route(Req(url)))
    def wait_for_timeout(self, *_args): pass
    def get_by_text(self, *_args, **_kwargs): return self
    def locator(self, *_args, **_kwargs): return self
    def count(self): return 1
    @property
    def first(self): return self
    def click(self, **_kwargs): pass


class Context:
    def __init__(self, urls): self.page = Page(urls)
    def new_page(self): return self.page
    def close(self): pass


class Chromium:
    def __init__(self, urls): self.urls = urls
    def launch_persistent_context(self, *_args, **_kwargs): return Context(self.urls)


class PW:
    def __init__(self, urls): self.chromium = Chromium(urls)
    def __enter__(self): return self
    def __exit__(self, *_args): pass


def factory(urls): return lambda: PW(urls)


def test_browser_dry_run_captures_two_urls_without_logging_signed_values(tmp_path, monkeypatch):
    monkeypatch.setattr("acquisition.cfaz_browser_session.profile_dir", lambda: tmp_path)
    output = []
    urls = [
        "https://storage.googleapis.com/a.zip?Signature=secret",
        "https://storage.googleapis.com/b.zip?Expires=1",
    ]
    session = CfazBrowserSession(output=output.append, playwright_factory=factory(urls))
    try:
        session.resolve(request_id="26977444", model_id="658742", expected_stl_file_ids={"1511267", "1511268"})
    except CfazBrowserSessionError:
        pass
    assert "secret" not in " ".join(output)


def test_url_map_requires_explicit_identity():
    result = CfazBrowserSession.validate_url_map(
        {"1511268": "https://storage.googleapis.com/b.zip?x=1",
         "1511267": "https://storage.googleapis.com/a.zip?x=2"},
        {"1511267", "1511268"})
    assert set(result) == {"1511267", "1511268"}


def test_browser_requires_exact_number_of_urls(tmp_path, monkeypatch):
    monkeypatch.setattr("acquisition.cfaz_browser_session.profile_dir", lambda: tmp_path)
    session = CfazBrowserSession(playwright_factory=factory([
        "https://storage.googleapis.com/a.zip?Signature=secret",
    ]))
    try:
        session.resolve(request_id="26977444", model_id="658742", expected_stl_file_ids={"1511267", "1511268"})
    except CfazBrowserSessionError:
        pass
    else:
        raise AssertionError("expected incomplete resolution")

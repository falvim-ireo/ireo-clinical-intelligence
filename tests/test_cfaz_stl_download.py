from io import BytesIO
from pathlib import Path
import struct
import zipfile

import pytest
import requests

from acquisition.cfaz_provider import CfazProvider
from acquisition.cfaz_stl_download import (
    CfazStlDownloadError,
    download_stl_diagnostic,
    download_stl_pair_diagnostic,
    validate_stl,
)


SYNTHETIC_URL = "https://files.example.invalid/model"


def binary_stl(*, declared_triangles=1, body_triangles=1):
    facet = struct.pack(
        "<12fH",
        0.0, 0.0, 1.0,
        0.0, 0.0, 0.0,
        1.0, 0.0, 0.0,
        0.0, 1.0, 0.0,
        0,
    )
    return b"SYNTHETIC".ljust(80, b"\0") + struct.pack(
        "<I", declared_triangles
    ) + facet * body_triangles


def ascii_stl():
    return (
        b"solid synthetic\n"
        b"facet normal 0 0 1\n"
        b"outer loop\n"
        b"vertex 0 0 0\n"
        b"vertex 1 0 0\n"
        b"vertex 0 1 0\n"
        b"endloop\n"
        b"endfacet\n"
        b"endsolid synthetic\n"
    )


def zip_payload(*members):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return stream.getvalue()


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for position in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[position:position + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def post(self, *_args, **_kwargs):
        self.post_calls.append((_args, _kwargs))
        raise AssertionError("POST must never be used")


class RoutingSession(Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = dict(responses)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.responses[url]


@pytest.fixture(autouse=True)
def allow_reserved_test_domain(monkeypatch):
    monkeypatch.setattr(
        CfazProvider,
        "_validate_download_url",
        classmethod(lambda _cls, _url: None),
    )


@pytest.mark.parametrize(
    ("content", "expected_format"),
    [(binary_stl(), "binary"), (ascii_stl(), "ascii")],
)
def test_validates_synthetic_binary_and_ascii_stl(
    tmp_path, content, expected_format,
):
    path = tmp_path / "synthetic.stl"
    path.write_bytes(content)

    detected, count = validate_stl(path)

    assert detected == expected_format
    assert count == 1


@pytest.mark.parametrize(
    "content",
    [
        binary_stl()[:-1],
        binary_stl(declared_triangles=2, body_triangles=1),
        b"solid synthetic\nfacet normal 0 0 1\n",
    ],
)
def test_rejects_truncated_or_incompatible_stl(tmp_path, content):
    path = tmp_path / "invalid.stl"
    path.write_bytes(content)

    with pytest.raises(CfazStlDownloadError):
        validate_stl(path)


def run_download(response, *, max_bytes=1024):
    session = Session(response)
    with download_stl_diagnostic(
        SYNTHETIC_URL, session=session, max_file_bytes=max_bytes,
    ) as result:
        snapshot = result
    return snapshot, session


def test_streams_valid_stl_with_one_get_and_no_post():
    result, session = run_download(Response(
        binary_stl(), headers={"Content-Type": "application/octet-stream"},
    ))

    assert result.stl_format == "binary"
    assert result.element_count == 1
    assert len(session.get_calls) == 1
    assert session.post_calls == []


def test_accepts_safe_zip_with_exactly_one_valid_stl():
    result, _session = run_download(Response(
        zip_payload(("synthetic.stl", ascii_stl())),
        headers={"Content-Type": "application/zip"},
    ))

    assert result.content_category == "zip"
    assert result.stl_format == "ascii"


def test_rejects_html_with_http_200():
    session = Session(Response(
        b"<html>synthetic</html>",
        headers={"Content-Type": "text/html"},
    ))

    with pytest.raises(CfazStlDownloadError):
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=session, max_file_bytes=1024,
        ):
            pass


def test_rejects_non_200_response():
    session = Session(Response(status=404))

    with pytest.raises(CfazStlDownloadError, match="HTTP 404"):
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=session, max_file_bytes=1024,
        ):
            pass


def test_rejects_declared_and_streamed_size_excess():
    declared = Session(Response(
        binary_stl(), headers={"Content-Length": "2048"},
    ))
    streamed = Session(Response(binary_stl()))

    with pytest.raises(CfazStlDownloadError, match="limite"):
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=declared, max_file_bytes=1024,
        ):
            pass
    with pytest.raises(CfazStlDownloadError, match="streaming"):
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=streamed, max_file_bytes=32,
        ):
            pass


def test_timeout_is_sanitized_and_never_leaks_url():
    session = Session(error=requests.Timeout("sensitive transport detail"))

    with pytest.raises(CfazStlDownloadError) as captured:
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=session, max_file_bytes=1024,
        ):
            pass

    assert "https://" not in str(captured.value)
    assert "sensitive" not in str(captured.value)


@pytest.mark.parametrize("success", [True, False])
def test_temporary_directory_is_removed_on_success_and_failure(
    tmp_path, monkeypatch, success,
):
    temporary = tmp_path / "isolated-diagnostic"
    def make_temporary(**_kwargs):
        temporary.mkdir()
        return str(temporary)

    monkeypatch.setattr(
        "acquisition.cfaz_stl_download.tempfile.mkdtemp",
        make_temporary,
    )
    response = Response(binary_stl() if success else b"<html></html>")
    session = Session(response)

    if success:
        with download_stl_diagnostic(
            SYNTHETIC_URL, session=session, max_file_bytes=1024,
        ):
            assert temporary.exists()
    else:
        with pytest.raises(CfazStlDownloadError):
            with download_stl_diagnostic(
                SYNTHETIC_URL, session=session, max_file_bytes=1024,
            ):
                pass

    assert not temporary.exists()


def test_pair_validates_two_identity_mapped_zips_in_inverted_map_order():
    url_alpha = "https://files.example.invalid/alpha"
    url_beta = "https://files.example.invalid/beta"
    session = RoutingSession({
        url_alpha: Response(
            zip_payload(("misleading-beta-name.stl", binary_stl())),
            headers={"Content-Type": "application/zip"},
        ),
        url_beta: Response(
            zip_payload(("misleading-alpha-name.stl", ascii_stl())),
            headers={"Content-Type": "application/zip"},
        ),
    })

    with download_stl_pair_diagnostic(
        {
            "synthetic-stl-beta": url_beta,
            "synthetic-stl-alpha": url_alpha,
        },
        expected_ids={"synthetic-stl-alpha", "synthetic-stl-beta"},
        session=session,
        max_file_bytes=2048,
    ) as results:
        assert results["synthetic-stl-alpha"].stl_format == "binary"
        assert results["synthetic-stl-beta"].stl_format == "ascii"

    assert len(session.get_calls) == 2
    assert session.post_calls == []


def test_pair_cleans_all_artifacts_when_second_file_is_invalid(
    tmp_path, monkeypatch,
):
    url_alpha = "https://files.example.invalid/alpha"
    url_beta = "https://files.example.invalid/beta"
    temporary = tmp_path / "pair-diagnostic"
    def make_temporary(**_kwargs):
        temporary.mkdir()
        return str(temporary)
    monkeypatch.setattr(
        "acquisition.cfaz_stl_download.tempfile.mkdtemp", make_temporary
    )
    session = RoutingSession({
        url_alpha: Response(zip_payload(("one.stl", binary_stl()))),
        url_beta: Response(zip_payload(("two.stl", b"truncated"))),
    })

    with pytest.raises(CfazStlDownloadError):
        with download_stl_pair_diagnostic(
            {
                "synthetic-stl-alpha": url_alpha,
                "synthetic-stl-beta": url_beta,
            },
            expected_ids={"synthetic-stl-alpha", "synthetic-stl-beta"},
            session=session,
            max_file_bytes=2048,
        ):
            pass

    assert len(session.get_calls) == 2
    assert not temporary.exists()


def test_pair_stops_after_invalid_first_file_without_second_get():
    url_alpha = "https://files.example.invalid/alpha"
    url_beta = "https://files.example.invalid/beta"
    session = RoutingSession({
        url_alpha: Response(zip_payload(("one.stl", b"invalid"))),
        url_beta: Response(zip_payload(("two.stl", binary_stl()))),
    })

    with pytest.raises(CfazStlDownloadError):
        with download_stl_pair_diagnostic(
            {
                "synthetic-stl-alpha": url_alpha,
                "synthetic-stl-beta": url_beta,
            },
            expected_ids={"synthetic-stl-alpha", "synthetic-stl-beta"},
            session=session,
            max_file_bytes=2048,
        ):
            pass

    assert len(session.get_calls) == 1
    assert session.get_calls[0][0] == url_alpha


def test_pair_rejects_shared_url_before_first_get():
    session = RoutingSession({})

    with pytest.raises(CfazStlDownloadError) as captured:
        with download_stl_pair_diagnostic(
            {
                "synthetic-stl-alpha": SYNTHETIC_URL,
                "synthetic-stl-beta": SYNTHETIC_URL,
            },
            expected_ids={"synthetic-stl-alpha", "synthetic-stl-beta"},
            session=session,
            max_file_bytes=2048,
        ):
            pass

    assert session.get_calls == []
    assert "https://" not in str(captured.value)


@pytest.mark.parametrize(
    "members",
    [
        [("readme.txt", b"synthetic")],
        [("one.stl", binary_stl()), ("two.stl", ascii_stl())],
    ],
)
def test_pair_rejects_zero_or_multiple_eligible_stl_members(members):
    url_alpha = "https://files.example.invalid/alpha"
    url_beta = "https://files.example.invalid/beta"
    session = RoutingSession({
        url_alpha: Response(zip_payload(*members)),
        url_beta: Response(zip_payload(("valid.stl", binary_stl()))),
    })

    with pytest.raises(CfazStlDownloadError):
        with download_stl_pair_diagnostic(
            {
                "synthetic-stl-alpha": url_alpha,
                "synthetic-stl-beta": url_beta,
            },
            expected_ids={"synthetic-stl-alpha", "synthetic-stl-beta"},
            session=session,
            max_file_bytes=4096,
        ):
            pass

    assert len(session.get_calls) == 1

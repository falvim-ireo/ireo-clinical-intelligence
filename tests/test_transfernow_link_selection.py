"""Seleção offline do link público TransferNow recebido por e-mail."""

from models.email_message import EmailMessage
import hashlib
import pytest
from urllib.parse import urlsplit

from integrations.transfernow_connector import TransferNowConnector
from radiology.transfernow_link_diagnosis import run_transfernow_link_diagnosis
from radiology.gmail_import import run_gmail_import
from radiology.transfernow_download import DownloadResult


@pytest.mark.parametrize(
    "url",
    ("https://sorrimagem.transfernow.net", "https://sorrimagem.transfernow.net/"),
)
def test_subdomain_root_is_rejected(url):
    with pytest.raises(ValueError, match="link público de transferência válido"):
        TransferNowConnector.interpretar(url)


@pytest.mark.parametrize("path", ("login", "signin", "privacy", "terms", "support"))
def test_non_public_transfernow_pages_are_rejected(path):
    with pytest.raises(ValueError, match="link público"):
        TransferNowConnector.interpretar(f"https://sorrimagem.transfernow.net/{path}")


def test_complete_public_link_preserves_path_and_query_internally():
    url = "https://sorrimagem.transfernow.net/dl/public-id?token=internal-value"
    message = TransferNowConnector.interpretar(url)
    assert message.download_url == url
    assert message.link_diagnosis.has_path is True
    assert message.link_diagnosis.has_query is True


def test_html_download_button_href_decodes_entities_and_wins_over_root():
    body = """
    <a href="https://sorrimagem.transfernow.net/">Logotipo</a>
    <a href="https://sorrimagem.transfernow.net/dl/public-id?a=1&amp;b=2">
      <span>Baixar</span>
    </a>
    """
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == (
        "https://sorrimagem.transfernow.net/dl/public-id?a=1&b=2"
    )
    assert message.link_diagnosis.candidate_type == "link /dl/"


def test_tracking_link_extracts_nested_transfernow_target():
    body = (
        '<a href="https://tracking.example/click?target='
        'https%3A%2F%2Ftransfernow.net%2Fdl%2Ftransfer-123%3Fx%3D1">'
        "View transfer</a>"
    )
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == "https://transfernow.net/dl/transfer-123?x=1"
    assert message.link_diagnosis.candidate_type == "link /dl/"


def test_public_button_wins_among_institutional_links():
    body = """
    <a href="https://sorrimagem.transfernow.net/">Início</a>
    <a href="https://sorrimagem.transfernow.net/privacy">Privacidade</a>
    <a href="https://social.example/sorrimagem">Rede social</a>
    <a href="https://transfernow.net/dl/the-transfer">Get your files</a>
    """
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == "https://transfernow.net/dl/the-transfer"
    assert message.link_diagnosis.total_links == 4
    assert message.link_diagnosis.public_candidates == 1


def test_no_public_candidate_has_sanitized_diagnosis():
    diagnosis = TransferNowConnector.diagnosticar_links(
        '<a href="https://sorrimagem.transfernow.net/">Logo</a>'
    )
    assert diagnosis.transfernow_links == 1
    assert diagnosis.public_candidates == 0
    assert diagnosis.candidate_type is None


def test_diagnosis_command_never_outputs_complete_url_or_token():
    secret_url = "https://transfernow.net/dl/secret-path?token=secret-token"
    message = EmailMessage(
        message_id="secret-message", subject="TransferNow", sender="TransferNow",
        reply_to=None, html_body=f'<a href="{secret_url}">Download</a>',
    )

    class Gmail:
        def list_messages(self, **kwargs): return [message]

    outputs = []
    run_transfernow_link_diagnosis(
        gmail=Gmail(), input_func=lambda _: "1", output=outputs.append
    )
    rendered = "\n".join(outputs)
    assert "transfernow.net" in rendered
    assert secret_url not in rendered
    assert "secret-path" not in rendered
    assert "secret-token" not in rendered
    assert "Presença de path: sim" in rendered
    assert "Presença de query: sim" in rendered


def test_access_download_button_forces_dl_url_over_subdomain_root():
    public = "https://sorrimagem.transfernow.net/dl/transfer-id/private-token"
    body = f"""
    <a href="https://sorrimagem.transfernow.net/">Logotipo</a>
    <a href="{public}"><span>Acessar o download</span></a>
    """
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == public
    assert message.link_diagnosis.dl_candidates == 1
    assert message.link_diagnosis.candidate_text == "Acessar o download"


def test_plain_dl_link_is_mandatory_choice_over_other_significant_link():
    public = "https://sorrimagem.transfernow.net/dl/id/token?source=email"
    body = (
        '<a href="https://sorrimagem.transfernow.net/news">Download novidades</a> '
        f"Link para destinatário: {public}"
    )
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == public
    assert urlsplit(message.download_url).path == "/dl/id/token"
    assert urlsplit(message.download_url).query == "source=email"


def test_duplicate_button_and_text_keep_one_public_candidate():
    public = "https://sorrimagem.transfernow.net/dl/id/token"
    body = f'<a href="{public}">Acessar o download</a> {public}'
    message = TransferNowConnector.interpretar(body)
    assert message.download_url == public
    assert message.link_diagnosis.public_candidates == 1


def test_root_only_email_is_rejected_before_downloader_or_browser(tmp_path):
    message = EmailMessage(
        message_id="id", subject="TransferNow", sender="TransferNow", reply_to=None,
        html_body=(
            '<a href="https://sorrimagem.transfernow.net/">Voltar para o início</a>'
        ),
    )

    class Gmail:
        def list_messages(self, **kwargs): return [message]

    class Forbidden:
        def download(self, *args): raise AssertionError("download/browser opened")
        def run(self, **kwargs): raise AssertionError("import started")

    with pytest.raises(ValueError, match="link público de transferência válido"):
        run_gmail_import(
            gmail=Gmail(), downloader=Forbidden(), browser_downloader=Forbidden(),
            importer=Forbidden(), quarantine_root=tmp_path,
            correlation_id="correlation-0001",
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("prompted")),
            output=lambda _: None,
        )


def test_selected_full_dl_url_is_tried_by_http_before_playwright(tmp_path):
    public = "https://sorrimagem.transfernow.net/dl/id/private-token?x=1"
    message = EmailMessage(
        message_id="id", subject='TransferNow "EXAME.rar"', sender="TransferNow",
        reply_to=None,
        html_body=f'<a href="{public}">Acessar o download</a>',
    )

    class Gmail:
        def list_messages(self, **kwargs): return [message]

    received = []
    archive = tmp_path / "EXAME.rar"

    class Http:
        def download(self, url, *args):
            received.append(url)
            archive.write_bytes(b"rar")
            return DownloadResult(archive, 3, hashlib.sha256(b"rar").hexdigest())

    class Browser:
        def download(self, *args): raise AssertionError("browser should not open")

    answers = iter(("1", "confirmar", ""))
    outputs = []
    run_gmail_import(
        gmail=Gmail(), downloader=Http(), browser_downloader=Browser(),
        importer=object(), quarantine_root=tmp_path,
        correlation_id="correlation-0001", input_func=lambda _: next(answers),
        output=outputs.append,
    )
    assert received == [public]
    assert all(public not in line and "private-token" not in line for line in outputs)

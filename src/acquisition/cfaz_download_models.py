from __future__ import annotations
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import tempfile, zipfile
from urllib.parse import urlsplit

class CfazDownloadModelsError(RuntimeError): pass

@contextmanager
def download_models(*, request_id: str, profile: Path, playwright_factory=None):
    factory = playwright_factory
    if factory is None:
        from playwright.sync_api import sync_playwright
        factory = sync_playwright
    with tempfile.TemporaryDirectory(prefix="ireo-cfaz-models-") as root:
        root_path = Path(root)
        with factory() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=False, accept_downloads=True)
            try:
                page = context.new_page()
                page.goto(f"https://max.cfaz.net/requests/{request_id}#digital_model", wait_until="networkidle", timeout=60000)
                try:
                    page.get_by_text("Modelo Digital", exact=False).first.click(timeout=5000)
                except Exception:
                    pass
                buttons = page.get_by_role("button", name="/baixar|download/i")
                count = buttons.count()
                if count != 2:
                    raise CfazDownloadModelsError("Não foram encontrados exatamente dois controles de download STL.")
                paths = []
                for index in range(2):
                    with page.expect_download(timeout=60000) as info:
                        buttons.nth(index).click()
                    target = root_path / f"download-{index}.bin"
                    info.value.save_as(str(target))
                    paths.append(target)
            finally:
                try: context.close()
                except Exception: pass
        yield tuple(paths)

def validate_downloads(paths):
    results = []
    for archive in paths:
        if archive.stat().st_size <= 0 or not zipfile.is_zipfile(archive):
            raise CfazDownloadModelsError("Download STL inválido.")
        with zipfile.ZipFile(archive) as zf:
            stls = [i for i in zf.infolist() if i.filename.casefold().endswith(".stl")]
            if len(stls) != 1:
                raise CfazDownloadModelsError("Cada download deve conter exatamente um STL.")
            data = zf.read(stls[0])
        results.append((sha256(data).hexdigest(), len(data)))
    if len(results) != 2 or results[0][0] == results[1][0]:
        raise CfazDownloadModelsError("Os conteúdos STL devem ser distintos.")
    return tuple(results)

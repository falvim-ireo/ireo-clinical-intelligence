"""Read-only browser fallback for Cfaz pages.

The browser profile is deliberately separate from the repository and is never
serialized as storage_state. Signed download URLs are kept only in memory.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable


def profile_dir() -> Path:
    try:
        from platformdirs import user_data_dir
        root = Path(user_data_dir("IREO Clinical Intelligence", "IREO"))
    except ImportError:
        root = Path.home() / ".local" / "share" / "IREO Clinical Intelligence"
    path = root / "cfaz-browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass
    return path


class CfazBrowserSessionError(RuntimeError):
    pass


class CfazBrowserSession:
    def __init__(self, *, output: Callable[[str], None] = print,
                 playwright_factory: Any = None) -> None:
        self.output = output
        self.playwright_factory = playwright_factory

    def login(self) -> None:
        factory = self.playwright_factory or self._factory()
        with factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir()), headless=False, accept_downloads=False,
            )
            try:
                page = context.new_page()
                page.goto("https://max.cfaz.net/", wait_until="domcontentloaded")
                self.output("Navegador aberto. Faça login diretamente no Cfaz.")
                page.wait_for_timeout(1000)
                page.wait_for_url("**/requests/**", timeout=300000)
                self.output("Sessão Cfaz autenticada e perfil salvo localmente.")
            finally:
                context.close()

    def resolve(self, *, request_id: str, model_id: str,
                expected_stl_file_ids: set[str] | tuple[str, ...] | list[str],
                manual_trigger: bool = False) -> dict[str, str]:
        expected_ids = {str(value) for value in expected_stl_file_ids}
        factory = self.playwright_factory or self._factory()
        urls: list[str] = []
        observed = 0
        initial = 0
        active = False
        post_requests = fetch_count = xhr_count = anchor_count = popup_count = download_count = blocked = 0
        with factory() as playwright:
            headless = False if manual_trigger else True
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir()), headless=headless, accept_downloads=False,
            )
            if manual_trigger and headless:
                raise CfazBrowserSessionError("Modo manual exige navegador visível.")
            try:
                page = context.new_page()
                def route_handler(route: Any) -> None:
                    nonlocal observed, initial
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.hostname in {"storage.googleapis.com", "storage.cloud.google.com"}:
                        observed += 1
                        if not active:
                            initial += 1
                            route.continue_()
                            return
                        if request.method == "GET":
                            path = parsed.path.casefold()
                            if manual_trigger or path.endswith((".zip", ".stl")):
                                # Keep the signed URL only in memory.  The
                                # signature is never included in diagnostics.
                                urls.append(request.url)
                                route.abort()
                                return
                    route.continue_()
                page.route("**/*", route_handler)
                # Keep browser-level observers armed before navigation. They
                # count only; signed URLs never reach output or disk.
                try:
                    context.on("request", lambda _request: None)
                    page.on("request", lambda _request: None)
                    page.on("download", lambda _download: None)
                    page.on("popup", lambda _popup: None)
                    page.add_init_script("""
                        (() => { const emit = (kind, url) => {
                          try { window.__ireoBrowserEvents = window.__ireoBrowserEvents || [];
                            window.__ireoBrowserEvents.push({kind, url: String(url || '')}); } catch (_) {}
                        };
                        const f = window.fetch; window.fetch = function(...a) { emit('fetch', a[0]); return f.apply(this,a); };
                        const xo = XMLHttpRequest.prototype.open; XMLHttpRequest.prototype.open = function(m,u,...r) { emit('xhr',u); return xo.call(this,m,u,...r); };
                        const wo = window.open; window.open = function(u,...a) { emit('popup',u); return wo.call(this,u,...a); };
                        const ac = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function() { emit('anchor',this.href); return ac.call(this); };
                        })();
                    """)
                except Exception:
                    pass
                page.goto(
                    f"https://max.cfaz.net/requests/{request_id}",
                    wait_until="networkidle", timeout=60000,
                )
                self._dom_diagnostics(page, model_id)
                self._identity_diagnostics(page, expected_ids)
                if manual_trigger:
                    active = True
                    self.output("Fase manual armada antes do prompt: sim")
                    self.output("Abra Modelo Digital e acione somente os downloads STL.")
                    input("Após acionar os dois downloads, pressione Enter: ")
                else:
                # The component is fragment-driven; clicking is best-effort.
                    selectors = (
                        f'[href="#digital_model{model_id}"]',
                        f'[data-target="#digital_model{model_id}"]',
                        f'[aria-controls="digital_model{model_id}"]',
                    )
                    for selector in selectors:
                        try:
                            active = True
                            page.locator(selector).first.click(timeout=3000)
                            break
                        except Exception:
                            pass
                    if not active:
                        raise CfazBrowserSessionError(
                            "Controle da seção Modelo Digital não foi identificado. "
                            "Use --manual-model-trigger."
                        )
                page.wait_for_timeout(2000)
                if not urls:
                    # Some Vue versions have no clickable tab because the
                    # fragment already selected it. Reload only after the
                    # initial inventory has been discarded.
                    active = True
                    page.goto(
                        f"https://max.cfaz.net/requests/{request_id}#digital_model{model_id}",
                        wait_until="networkidle", timeout=60000,
                    )
            except Exception as exc:
                raise CfazBrowserSessionError(
                    "Sessão Cfaz inexistente, expirada ou página indisponível."
                ) from exc
            finally:
                try:
                    context.close()
                except Exception as cleanup_error:
                    if "Connection closed" not in str(cleanup_error):
                        raise
        # Avoid accidentally counting unrelated storage requests.
        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            identity = urlsplit(url)._replace(query="", fragment="").geturl()
            if identity not in seen:
                seen.add(identity)
                unique.append(url)
        urls = unique
        self.output(f"Requisições Google Storage observadas: {observed}")
        self.output(f"Candidatas antes da seção: {initial}")
        self.output(f"Candidatas após da seção: {len(urls)}")
        self.output(f"Candidatas ZIP/STL: {len(urls)}")
        self.output(f"Requests pós-baseline: {max(0, observed-initial)}")
        self.output(f"Chamadas fetch pós-baseline: {fetch_count}")
        self.output(f"Chamadas XHR pós-baseline: {xhr_count}")
        self.output(f"Cliques anchor pós-baseline: {anchor_count}")
        self.output(f"Window.open/popup pós-baseline: {popup_count}")
        self.output(f"Eventos download pós-baseline: {download_count}")
        self.output("Chamadas associadas a unzipFileDownloadUrl: não expostas")
        self.output(f"Transferências bloqueadas: {len(urls)}")
        self.output("Arquivos salvos: 0")
        if len(urls) != len(expected_ids):
            raise CfazBrowserSessionError(
                f"Sessão browser resolveu {len(urls)} URL(s); esperado: {len(expected_ids)}."
            )
        self.output(f"URLs resolvidas: {len(urls)}")
        self.output("Arquivos salvos: 0")
        # A browser network request alone has no reliable STL identity. Never
        # infer it from capture order; callers must provide an explicit map.
        raise CfazBrowserSessionError(
            "As capturas browser não expõem stl_file_id de forma inequívoca; "
            "nenhum mapa URL→STL foi produzido."
        )

    def resolve_collection(self, specification, *, manual_model_trigger: bool = True,
                           stabilization_seconds: float = 2.0) -> frozenset[str]:
        if getattr(specification, "identity_scope", None) != "COLLECTION" or getattr(specification, "individual_source_mapping", None) is not None:
            raise CfazBrowserSessionError("Especificação não é uma coleção não individualizada.")
        if getattr(specification, "expected_member_count", None) != 2 or not manual_model_trigger:
            raise CfazBrowserSessionError("COLLECTION exige acionamento manual explícito.")
        factory = self.playwright_factory or self._factory()
        urls: set[str] = set()
        armed = False
        with factory() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile_dir()), headless=False, accept_downloads=False, service_workers="block")
            try:
                page = context.new_page()
                post_requests = eligible = rejected = 0
                def route_handler(route):
                    nonlocal post_requests, eligible, rejected
                    parsed = urlsplit(route.request.url)
                    if armed:
                        post_requests += 1
                        if route.request.method == "GET" and parsed.hostname in {"storage.googleapis.com", "storage.cloud.google.com"}:
                            eligible += 1
                            urls.add(route.request.url); route.abort(); return
                        rejected += 1
                    route.continue_()
                context.route("**/*", route_handler)
                page.goto(f"https://max.cfaz.net/requests/{specification.request_id}", wait_until="networkidle", timeout=60000)
                armed = True
                page.bring_to_front()
                self.output("Fase manual armada antes do prompt: sim")
                self.output("Acione exatamente os dois downloads STL no navegador visível.")
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                    if len(urls) >= 2:
                        page.wait_for_timeout(int(stabilization_seconds * 1000))
                        break
                self.output(f"Requests pós-armamento: {post_requests}")
                self.output(f"Requisições elegíveis: {eligible}")
                self.output(f"Rejeições genéricas: {rejected}")
            finally:
                try: context.close()
                except Exception as exc:
                    if "Connection closed" not in str(exc): raise
        if len(urls) != 2:
            raise CfazBrowserSessionError(f"Coleção resolveu {len(urls)} candidata(s); esperado: 2.")
        return frozenset(urls)

    @staticmethod
    def validate_url_map(url_map: object, expected_ids: set[str]) -> dict[str, str]:
        if not isinstance(url_map, dict):
            raise CfazBrowserSessionError("O resolvedor deve retornar um mapa stl_file_id→URL.")
        normalized = {str(key): value for key, value in url_map.items()}
        if set(normalized) != {str(value) for value in expected_ids}:
            raise CfazBrowserSessionError("Mapa de modelos incompleto ou com chaves adicionais.")
        values = list(normalized.values())
        if any(not isinstance(value, str) or not value.startswith("https://") for value in values):
            raise CfazBrowserSessionError("Mapa contém URL inválida.")
        if len(set(values)) != len(values):
            raise CfazBrowserSessionError("Dois STL foram associados à mesma URL.")
        return {key: str(value) for key, value in normalized.items()}

    def _dom_diagnostics(self, page: Any, model_id: str) -> None:
        selectors = (
            f'[href="#digital_model{model_id}"]',
            f'[data-target="#digital_model{model_id}"]',
            f'[aria-controls="digital_model{model_id}"]',
            f'#{model_id}',
        )
        counts = []
        for selector in selectors:
            try:
                counts.append(str(page.locator(selector).count()))
            except Exception:
                counts.append("0")
        self.output("Controles Modelo Digital encontrados: " + ",".join(counts))

    def _identity_diagnostics(self, page: Any, expected_ids: set[str]) -> None:
        """Inspect only technical identity markers; never dump DOM/state."""
        try:
            result = page.evaluate("""(ids) => {
              const wanted = new Set(ids), found = new Set(), sources = new Set();
              const scan = (node, source) => {
                if (!node || typeof node !== 'object') return;
                const vals = [];
                for (const k of ['id','stl_file_id','stlFileId','fileId','data-stl-file-id']) {
                  try { if (node.getAttribute && node.hasAttribute(k)) vals.push(node.getAttribute(k)); } catch (_) {}
                  try { if (node.dataset && node.dataset[k]) vals.push(node.dataset[k]); } catch (_) {}
                }
                for (const value of vals) if (wanted.has(String(value))) { found.add(String(value)); sources.add(source); }
              };
              document.querySelectorAll('*').forEach((node) => scan(node, 'dom'));
              return {found: Array.from(found), sources: Array.from(sources)};
            }""", sorted(expected_ids))
            found = result.get("found", []) if isinstance(result, dict) else []
            sources = result.get("sources", []) if isinstance(result, dict) else []
            self.output("Identidade no DOM: " + ("sim" if found else "não"))
            self.output("Fontes técnicas no DOM: " + ",".join(sorted(sources)))
            self.output("IDs esperados encontrados no DOM: " + str(len(found)))
        except Exception:
            self.output("Identidade no DOM: não")

    @staticmethod
    def _factory():
        from playwright.sync_api import sync_playwright
        return sync_playwright

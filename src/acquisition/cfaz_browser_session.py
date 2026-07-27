"""Read-only browser fallback for Cfaz pages.

The browser profile is deliberately separate from the repository and is never
serialized as storage_state. Signed download URLs are kept only in memory.
"""
from __future__ import annotations

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
        if not expected_ids:
            raise CfazBrowserSessionError(
                "Nenhum identificador STL foi informado ao resolvedor."
            )
        factory = self.playwright_factory or self._factory()
        with factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir()), headless=True, accept_downloads=False,
                service_workers="block",
            )
            try:
                page = context.new_page()
                page.goto(
                    f"https://max.cfaz.net/requests/{request_id}",
                    wait_until="networkidle", timeout=60000,
                )
                section = page.locator(f"#digital_model{model_id}")
                if not (section.count() and section.first.is_visible()):
                    selectors = (
                        f'[href="#digital_model{model_id}"]',
                        f'[data-target="#digital_model{model_id}"]',
                        f'[aria-controls="digital_model{model_id}"]',
                    )
                    controls = page.locator(",".join(selectors))
                    visible = [
                        controls.nth(position)
                        for position in range(controls.count())
                        if controls.nth(position).is_visible()
                    ]
                    if len(visible) != 1:
                        raise CfazBrowserSessionError(
                            "A seção de modelos digitais não pôde ser identificada "
                            "inequivocamente."
                        )
                    visible[0].click(timeout=3000)
                    page.wait_for_timeout(500)
                controls = page.evaluate(
                    """() => Array.from(
                      document.querySelectorAll('button[data-download-url]')
                    ).map((node) => ({
                      element: node.tagName.toLowerCase(),
                      stl_file_id: node.getAttribute('data-id'),
                      model_id: node.getAttribute('data-model-id'),
                      download_url: node.getAttribute('data-download-url'),
                    }))"""
                )
                resolved = self.resolve_declared_url_map(
                    controls, expected_ids=expected_ids
                )
            except Exception as exc:
                if isinstance(exc, CfazBrowserSessionError):
                    raise
                raise CfazBrowserSessionError(
                    "Sessão Cfaz inexistente, expirada ou página indisponível."
                ) from exc
            finally:
                try:
                    context.close()
                except Exception as cleanup_error:
                    if "Connection closed" not in str(cleanup_error):
                        raise
        self.output(f"Associações STL resolvidas: {len(resolved)}")
        self.output("Cliques em controles de arquivo: 0")
        self.output("Requisições de transferência: 0")
        self.output("Arquivos salvos: 0")
        return resolved

    @classmethod
    def resolve_declared_url_map(
        cls, controls: object, *, expected_ids: set[str]
    ) -> dict[str, str]:
        """Resolve somente button[data-id][data-download-url] sem heurísticas."""
        expected = {str(value) for value in expected_ids}
        if not isinstance(controls, list):
            raise CfazBrowserSessionError(
                "Controles de modelos retornaram estrutura inválida."
            )
        matches: dict[str, list[str]] = {value: [] for value in expected}
        for control in controls:
            if not isinstance(control, dict):
                raise CfazBrowserSessionError(
                    "Controle de modelo sem estrutura identificável."
                )
            if str(control.get("element") or "").casefold() != "button":
                continue
            identity = str(control.get("stl_file_id") or "").strip()
            model_identity = str(control.get("model_id") or "").strip()
            url = str(control.get("download_url") or "").strip()
            if identity not in expected or model_identity:
                continue
            if not url:
                raise CfazBrowserSessionError(
                    "Botão de arquivo sem URL declarativa."
                )
            if not url.startswith("https://"):
                raise CfazBrowserSessionError(
                    "Botão de arquivo contém URL estruturalmente inválida."
                )
            matches[identity].append(url)
        if any(len(values) != 1 for values in matches.values()):
            raise CfazBrowserSessionError(
                "Associação STL incompleta ou ambígua; revisão obrigatória."
            )
        resolved = {
            identity: values[0] for identity, values in matches.items()
        }
        if len(set(resolved.values())) != len(resolved):
            raise CfazBrowserSessionError(
                "Uma URL foi associada a mais de um STL."
            )
        return cls.validate_url_map(resolved, expected)

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

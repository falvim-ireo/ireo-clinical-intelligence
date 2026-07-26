"""Aquisição Cfaz via API oficial, isolada das regras clínicas e de publicação."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Callable
from dataclasses import dataclass, field as dataclass_field, replace
from urllib.parse import parse_qsl, unquote, urlsplit
from html import unescape
import zipfile

import requests

from acquisition.base import (
    AcquiredPackage, AcquisitionAsset, AcquisitionError, AcquisitionProvider,
    AcquisitionRequest, AssetClassification,
)
from services.patient_normalizer import PatientNormalizer


class CfazAuthenticationError(AcquisitionError): pass
class CfazRequestError(AcquisitionError): pass
class CfazEmptyRequestError(AcquisitionError): pass


class CfazHTTPError(CfazRequestError):
    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(
            f"O Cfaz retornou erro ao consultar o pedido (HTTP {self.status_code})."
        )


class CfazAmbiguousRequestError(CfazRequestError): pass


@dataclass(frozen=True)
class CfazDigitalModelFile:
    """Referência efêmera a um arquivo de modelo digital do Cfaz."""

    digital_model_id: str
    stl_file_id: str
    download_url: str = dataclass_field(repr=False)
    filename: str | None = None
    model_name: str | None = None
    source_field: str | None = None


@dataclass(frozen=True)
class CfazDigitalModelInventory:
    request: AcquisitionRequest
    model_count: int
    files: tuple[CfazDigitalModelFile, ...]


class CfazProvider(AcquisitionProvider):
    provider_id = "cfaz"
    provider_name = "Cfaz.net"
    BASE_URL = "https://max.cfaz.net"
    API_ROOT = f"{BASE_URL}/api/v1"
    REQUEST_URL = re.compile(
        r"https://max\.cfaz\.net/(?:requests|requests_with_token)/(\d+)", re.I
    )
    ALLOWED_DOWNLOAD_SUFFIXES = (
        "cfaz.net", "googleapis.com", "googleusercontent.com",
    )
    LOOKUP_MAX_PAGES = 20
    LOOKUP_PER_PAGE = 100
    LOOKUP_DAYS = 365

    def __init__(
        self, *, api_token: str | None = None, email: str | None = None,
        password: str | None = None, session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        max_file_bytes: int = 2 * 1024 * 1024 * 1024,
        output: Callable[[str], None] = print,
        auth_diagnostics: bool = False,
        payload_diagnostics: bool = False,
        now_provider: Callable[[], datetime] | None = None,
        browser_resolver: Callable[..., list[str]] | None = None,
    ) -> None:
        self._api_token = str(api_token or "").strip() or None
        self._email = str(email or "").strip() or None
        self._password = password or None
        self._session = session or requests.Session()
        self.timeout = timeout
        self.max_file_bytes = max_file_bytes
        self.output = output
        self.auth_diagnostics = bool(auth_diagnostics)
        self.payload_diagnostics = bool(payload_diagnostics)
        self._auth_headers: dict[str, str] = {}
        self._request_exam_counts: dict[str, int] = {}
        self._request_payloads: dict[str, dict[str, Any]] = {}
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._browser_resolver = browser_resolver

    def authenticate(self) -> None:
        if self._api_token:
            self._validate_fixed_api_token(self._api_token)
            self._auth_headers = {"Authorization": f"Token {self._api_token}"}
            if self.auth_diagnostics:
                self.output("Login URL: não aplicável (token de API configurado)")
                self.output("Login HTTP Status: não aplicável")
                self.output("Login Content-Type: não aplicável")
                self.output("Token recebido: sim (configuração local)")
                self.output("Cookie recebido: não")
                self._diagnose_configured_auth()
            return
        self._authenticate_with_credentials()

    def _authenticate_with_credentials(self) -> None:
        if not self._email or not self._password:
            raise CfazAuthenticationError("Credenciais do Cfaz não foram configuradas.")
        try:
            response = self._session.post(
                f"{self.API_ROOT}/auth/sign_in",
                data={"email": self._email, "password": self._password},
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise CfazAuthenticationError("Tempo limite excedido no login do Cfaz.") from None
        except requests.RequestException:
            raise CfazAuthenticationError("Falha de comunicação no login do Cfaz.") from None
        if self.auth_diagnostics:
            self._diagnose_login_response(response)
            if response.status_code == 401:
                self._diagnose_unauthorized(response)
        if not 200 <= response.status_code < 300:
            raise CfazAuthenticationError(
                f"Login do Cfaz rejeitado (HTTP {response.status_code})."
            )
        required = ("access-token", "client", "uid", "expiry")
        headers = {name: response.headers.get(name) for name in required}
        if not all(headers.values()):
            raise CfazAuthenticationError("Login do Cfaz não retornou uma sessão válida.")
        self._auth_headers = {
            "access-token": str(headers["access-token"]),
            "token-type": response.headers.get("token-type", "Bearer"),
            "client": str(headers["client"]),
            "uid": str(headers["uid"]),
            "expiry": str(headers["expiry"]),
        }
        if self.auth_diagnostics:
            self._diagnose_configured_auth()

    def discover(self, notification: Any) -> tuple[AcquisitionRequest, ...]:
        return self.discover_request(self._request_id_from_notification(notification))

    def discover_request(self, request_id: str) -> tuple[AcquisitionRequest, ...]:
        """Aceita ID interno ou número visível e resolve somente por rotas públicas."""
        supplied_id = str(request_id or "").strip()
        if not re.fullmatch(r"\d+", supplied_id):
            raise CfazRequestError("O Request ID do Cfaz é inválido.")
        if not self._auth_headers:
            self.authenticate()
        resolved_from_sequential = False
        try:
            payload = self._api_get(f"/requests/{supplied_id}")
            provider_request_id = supplied_id
        except CfazHTTPError as exc:
            if exc.status_code != 404:
                raise
            provider_request_id = self._resolve_sequential_id(supplied_id)
            resolved_from_sequential = True
            payload = self._api_get(f"/requests/{provider_request_id}")
        if not isinstance(payload, dict):
            raise CfazRequestError("O pedido Cfaz retornou uma estrutura inválida.")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise CfazRequestError("O pedido Cfaz retornou uma estrutura inválida.")
        if self.payload_diagnostics:
            self._diagnose_payload_structure(payload, data)
        actual_id = str(data.get("id") or provider_request_id)
        if actual_id != provider_request_id:
            raise CfazRequestError("O Cfaz retornou um pedido diferente do solicitado.")
        sequential_id = self._optional_identifier(data.get("sequential_id"))
        if resolved_from_sequential and sequential_id != supplied_id:
            raise CfazRequestError("O Cfaz retornou um número visível diferente do solicitado.")
        clinic_number = self._clinic_number(data)
        self._request_payloads[actual_id] = data
        self.output(f"Pedido visível: {sequential_id or supplied_id}")
        self.output(f"ID interno resolvido: {actual_id}")
        self.output(f"Nº Clínica: {clinic_number or 'não informado'}")
        patient = data.get("patient_datum") if isinstance(data.get("patient_datum"), dict) else {}
        dentist = data.get("dentist_datum") if isinstance(data.get("dentist_datum"), dict) else {}
        clinic = data.get("clinic") if isinstance(data.get("clinic"), dict) else {}
        assets = tuple(self._assets(data))
        self._request_exam_counts[actual_id] = self._exam_count(data)
        canonical_url = f"{self.BASE_URL}/requests/{actual_id}"
        return (AcquisitionRequest(
            provider_id=self.provider_id,
            request_id=actual_id,
            provider_exam_id=self._provider_exam_id(data),
            provider_request_id=actual_id,
            sequential_id=sequential_id,
            clinic_number=clinic_number,
            source_url=canonical_url,
            patient_name=self._optional_text(patient.get("name")),
            request_date=self._datetime(data.get("created_at")),
            exam_date=self._datetime(data.get("date")),
            radiology_clinic=self._optional_text(clinic.get("name")),
            professional=self._optional_text(dentist.get("name")),
            assets=assets,
        ),)

    def discover_digital_models(
        self, request_id: str
    ) -> CfazDigitalModelInventory:
        """Resolve os ZIPs/STL expostos pela página autenticada do pedido."""
        request = self.discover_request(request_id)[0]
        payload = self._request_payloads.get(request.request_id)
        if not isinstance(payload, dict):
            raise CfazRequestError(
                "Os metadados do pedido Cfaz não ficaram disponíveis para os modelos."
            )
        descriptors: list[dict[str, Any]] = []
        model_links: dict[str, str] = {}
        models = payload.get("digital_models")
        if not isinstance(models, list):
            models = []
        for model_position, model in enumerate(models, 1):
            if not isinstance(model, dict):
                continue
            model_id = self._optional_identifier(model.get("id"))
            if not model_id:
                raise CfazRequestError(
                    "O Cfaz retornou um modelo digital sem identificador."
                )
            model_link = self._optional_https_url(model.get("link") or model.get("digital_model_link"))
            if model_link:
                model_links[model_id] = model_link
            model_name = self._optional_text(model.get("model_name"))
            stl_files = model.get("stl_files")
            if not isinstance(stl_files, list):
                stl_files = []
            for file_position, item in enumerate(stl_files, 1):
                if not isinstance(item, dict):
                    continue
                stl_file_id = self._optional_identifier(item.get("id"))
                if not stl_file_id:
                    raise CfazRequestError(
                        "O Cfaz retornou um arquivo STL sem identificador."
                    )
                direct_url = self._optional_https_url(
                    item.get("download_url")
                    or item.get("url")
                    or item.get("link")
                )
                descriptors.append({
                    "digital_model_id": model_id,
                    "stl_file_id": stl_file_id,
                    "model_name": model_name,
                    "filename": self._optional_text(
                        item.get("document_file_name")
                        or item.get("filename")
                        or item.get("name")
                    ),
                    "download_url": direct_url,
                    "source_field": (
                        f"digital_models[{model_position}]."
                        f"stl_files[{file_position}]"
                    ),
                })
        if self.payload_diagnostics and descriptors:
            field_names = sorted({key for item in descriptors for key in item if key not in {"download_url"}})
            unique_ids = len({str(item["stl_file_id"]) for item in descriptors})
            self.output("Campos técnicos STL: " + ",".join(field_names))
            self.output(f"IDs STL únicos: {unique_ids}/{len(descriptors)}")
        if not descriptors:
            return CfazDigitalModelInventory(
                request=request, model_count=len(models), files=()
            )
        if any(not item["download_url"] for item in descriptors):
            page_payload = self._page_get(
                f"/requests/{request.request_id}.json"
            )
            candidates = self._digital_model_url_candidates(page_payload)
            # A resposta do pedido contém apenas os IDs. A página de detalhe
            # do modelo é a fonte autenticada dos links assinados.
            unresolved_ids = {
                str(item["stl_file_id"]) for item in descriptors if not item["download_url"]
            }
            page_ids = {str(item.get("stl_file_id")) for item in candidates if item.get("stl_file_id")}
            for model_id in sorted({item["digital_model_id"] for item in descriptors}):
                if unresolved_ids.issubset(page_ids):
                    break
                try:
                    model_payload = self._page_get(
                        f"/digital_models/{model_id}.json"
                    )
                except CfazHTTPError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                model_candidates = self._digital_model_url_candidates(model_payload)
                if not model_candidates:
                    model_link = model_links.get(model_id)
                    if model_link:
                        model_payload = self._page_get(model_link)
                        model_candidates = self._digital_model_url_candidates(model_payload)
                if not model_candidates:
                    try:
                        model_payload = self._page_get(f"/digital_models/{model_id}")
                    except CfazHTTPError as exc:
                        if exc.status_code == 404:
                            continue
                        raise
                    model_candidates = self._digital_model_url_candidates(model_payload)
                candidates.extend(model_candidates)
            self._assign_digital_model_urls(descriptors, candidates)
        unresolved = [
            str(item["stl_file_id"])
            for item in descriptors if not item["download_url"]
        ]
        if unresolved:
            if self._browser_resolver:
                browser_urls = self._browser_resolver(
                    request_id=request.request_id,
                    model_id=str(descriptors[0]["digital_model_id"]),
                    expected_stl_file_ids={str(item["stl_file_id"]) for item in descriptors},
                )
                if not isinstance(browser_urls, dict):
                    raise CfazRequestError(
                        "As URLs do navegador não estão associadas inequivocamente "
                        "aos stl_file_id; associação por ordem foi bloqueada."
                    )
                for descriptor in descriptors:
                    url = browser_urls.get(str(descriptor["stl_file_id"]))
                    if url:
                        descriptor["download_url"] = str(url)
                        descriptor["source_field"] = "browser:unzipFileDownloadUrl"
                unresolved = [
                    str(item["stl_file_id"])
                    for item in descriptors if not item["download_url"]
                ]
        if unresolved:
            raise CfazRequestError(
                "A página autenticada do Cfaz não forneceu todos os downloads "
                "dos modelos digitais."
            )
        files = []
        for item in descriptors:
            url = str(item["download_url"])
            self._validate_download_url(url)
            files.append(CfazDigitalModelFile(
                digital_model_id=str(item["digital_model_id"]),
                stl_file_id=str(item["stl_file_id"]),
                download_url=url,
                filename=(
                    self._optional_text(item.get("filename"))
                    or self._filename_from({}, url)
                ),
                model_name=self._optional_text(item.get("model_name")),
                source_field=self._optional_text(item.get("source_field")),
            ))
        return CfazDigitalModelInventory(
            request=request,
            model_count=len(models),
            files=tuple(files),
        )

    def _resolve_sequential_id(self, sequential_id: str) -> str:
        since = self._now_provider()
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_value = (since - timedelta(days=self.LOOKUP_DAYS)).isoformat()
        matches: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()
        for page in range(1, self.LOOKUP_MAX_PAGES + 1):
            payload = self._api_get("/requests", params={
                "page": page,
                "per_page": self.LOOKUP_PER_PAGE,
                "q[created_at_gteq]": since_value,
            })
            items = self._request_list_items(payload)
            if self.payload_diagnostics:
                self.output(
                    f"Resolução sequential_id: página {page}, "
                    f"pedidos recebidos={len(items)}"
                )
            if not items:
                break
            new_on_page = 0
            for item in items:
                internal_id = self._optional_identifier(item.get("id"))
                if not internal_id or internal_id in seen_ids:
                    continue
                seen_ids.add(internal_id)
                new_on_page += 1
                if self._optional_identifier(item.get("sequential_id")) == sequential_id:
                    matches[internal_id] = item
            if new_on_page == 0:
                break
            if self._is_last_page(payload, page, len(items)):
                break
        if not matches:
            raise CfazRequestError(
                "Nenhum pedido recente corresponde ao número visível informado."
            )
        if len(matches) != 1:
            raise CfazAmbiguousRequestError(
                "Mais de um pedido corresponde ao número visível informado; "
                "revisão manual obrigatória."
            )
        return next(iter(matches))

    @staticmethod
    def _request_list_items(payload: Any) -> list[dict[str, Any]]:
        values = payload
        if isinstance(payload, dict):
            for key in ("data", "requests", "results", "items"):
                if isinstance(payload.get(key), list):
                    values = payload[key]
                    break
        if not isinstance(values, list):
            raise CfazRequestError("A listagem de pedidos retornou estrutura inválida.")
        return [item for item in values if isinstance(item, dict)]

    @classmethod
    def _is_last_page(cls, payload: Any, page: int, count: int) -> bool:
        if isinstance(payload, dict):
            pagination = payload.get("pagination")
            if isinstance(pagination, dict):
                total_pages = pagination.get("total_pages") or pagination.get("pages")
                try:
                    return page >= int(total_pages)
                except (TypeError, ValueError):
                    pass
            if payload.get("next_page") is None and "next_page" in payload:
                return True
        return count < cls.LOOKUP_PER_PAGE

    def download(
        self, request: AcquisitionRequest, quarantine_root: str | Path,
        correlation_id: str,
    ) -> AcquiredPackage:
        if not request.assets:
            exam_count = self._request_exam_counts.get(request.request_id, 0)
            if exam_count == 0:
                raise CfazEmptyRequestError(
                    "Pedido sem exames: o pedido Cfaz não possui arquivos "
                    "disponíveis nem exames."
                )
            raise CfazEmptyRequestError(
                f"Exames existentes sem arquivos: o pedido Cfaz possui "
                f"{exam_count} exame(s), mas nenhum arquivo direto foi "
                "disponibilizado pela API."
            )
        root = Path(quarantine_root).expanduser().resolve()
        request_folder = (root / correlation_id / f"cfaz-{request.request_id}").resolve()
        if not request_folder.is_relative_to(root):
            raise CfazRequestError("Destino de quarentena inválido.")
        request_folder.mkdir(parents=True, exist_ok=True)
        state_path = request_folder / ".cfaz-download-state.json"
        state = self._read_state(state_path, request.request_id)
        downloaded: list[tuple[AcquisitionAsset, Path, str, int, dict[str, Any]]] = []
        digest_paths: dict[str, Path] = {}
        resumed = 0
        used_names: set[str] = set()
        visible_names: set[str] = set()
        name_counters: dict[str, int] = {}
        for position, asset in enumerate(request.assets, 1):
            prior = state.get(asset.asset_id)
            prior_filename = (
                str(prior.get("filename") or "")
                if isinstance(prior, dict) else ""
            )
            filename = (
                self._safe_filename(prior_filename) if prior_filename
                else self._unique_filename(asset, position, used_names)
            )
            used_names.add(filename.casefold())
            destination = request_folder / filename
            if (
                isinstance(prior, dict) and prior.get("status") == "VIEW_LINK"
                and asset.probe_html
            ):
                resumed += 1
                continue
            if (
                isinstance(prior, dict) and prior.get("status") == "DUPLICATE"
                and str(prior.get("sha256") or "") in digest_paths
            ):
                resumed += 1
                continue
            if self._completed_file(destination, prior):
                digest = str(prior["sha256"])
                size = int(prior["size"])
                mime_type = self._file_mime(destination)
                resumed += 1
            else:
                self.output(f"Baixando {asset.classification.value.lower()}.........")
                result = self._download_asset(asset, destination)
                if result is None:
                    state[asset.asset_id] = {"status": "VIEW_LINK"}
                    self._write_state(state_path, request.request_id, state)
                    self.output("Link de visualização HTML ignorado com segurança.")
                    continue
                digest, size, mime_type = result
            refined_asset = replace(
                asset,
                classification=(
                    self._classification_from_state(prior)
                    or self._refine_classification(asset, mime_type, destination)
                ),
            )
            if digest in digest_paths:
                destination.unlink(missing_ok=True)
                state[asset.asset_id] = {
                    "status": "DUPLICATE", "sha256": digest, "size": size,
                }
                self._write_state(state_path, request.request_id, state)
                self.output("Arquivo duplicado por SHA-256 ignorado.")
                continue
            stored_name = self._stored_filename(
                refined_asset, mime_type, name_counters, visible_names
            )
            stored_path = request_folder / stored_name
            if destination != stored_path:
                if stored_path.exists():
                    if self._sha256(stored_path) != digest:
                        raise CfazRequestError(
                            "Nome local determinístico conflita com outro arquivo."
                        )
                    destination.unlink(missing_ok=True)
                else:
                    destination.replace(stored_path)
                destination = stored_path
            width, height = self._image_dimensions(destination, mime_type)
            collection = self._asset_collection(asset)
            is_thumbnail = bool(
                width and height and max(width, height) <= 320
            )
            file_metadata = {
                "original_source": asset.source_field or "campo não informado",
                "source_collection": collection,
                "provider_section": asset.source_field,
                "provider_display_name": refined_asset.classification.value,
                "provider_exam_id": asset.provider_exam_id,
                "provider_asset_id": asset.provider_asset_id,
                "clinical_category": (
                    "DIGITAL_MODEL"
                    if refined_asset.classification == AssetClassification.DIGITAL_MODEL
                    else None
                ),
                "provider_metadata": {
                    "provider": request.provider_id,
                    "provider_request_id": request.provider_request_id or request.request_id,
                },
                "source_name": asset.filename,
                "source_url_hash": hashlib.sha256(
                    asset.download_url.encode("utf-8")
                ).hexdigest(),
                "stored_name": stored_name,
                "detected_mime": mime_type,
                "extension": Path(stored_name).suffix.casefold(),
                "width": width,
                "height": height,
                "sha256": digest,
                "collection": collection,
                "is_thumbnail": is_thumbnail,
                "downloaded_at": self._now_provider().astimezone(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            }
            state[asset.asset_id] = {
                "status": "COMPLETE", "filename": stored_name,
                "sha256": digest, "size": size, "mime_type": mime_type,
                "classification": refined_asset.classification.value,
            }
            self._write_state(state_path, request.request_id, state)
            digest_paths[digest] = destination
            self.output(f"Baixando {refined_asset.classification.value.lower()}......... OK")
            downloaded.append((refined_asset, destination, digest, size, file_metadata))
        if not downloaded:
            raise CfazEmptyRequestError(
                "Exames existentes sem arquivos: os candidatos retornaram apenas "
                "links HTML de visualização ou arquivos duplicados indisponíveis."
            )
        downloaded = self._without_redundant_thumbnails(downloaded)
        refined_request = replace(request, assets=tuple(item[0] for item in downloaded))
        archive = request_folder / self._archive_name(request)
        self._write_deterministic_zip(archive, downloaded)
        archive_sha = self._sha256(archive)
        return AcquiredPackage(
            request=refined_request, archive_path=archive, sha256=archive_sha,
            file_count=len(downloaded), total_bytes=sum(item[3] for item in downloaded),
            resumed_files=resumed,
            file_metadata=tuple(dict(item[4]) for item in downloaded),
        )

    def classify(self, filename: str, metadata: dict[str, Any] | None = None) -> AssetClassification:
        value = PatientNormalizer.compare_ready(" ".join((
            filename, str((metadata or {}).get("model_name") or ""),
            str((metadata or {}).get("type") or ""),
        )))
        rules = (
            (("PANORAM", "ORTOPANTOM"), AssetClassification.PANORAMIC),
            (("TELERRAD", "CEFALOMETR"), AssetClassification.TELERADIOGRAPHY),
            (("PERIAP",), AssetClassification.PERIAPICAL_SERIES),
            (("BITE WING", "BITEWING", "INTERPROX"), AssetClassification.BITE_WING),
            (("FOTOGRAF", "FOTO CLIN"), AssetClassification.CLINICAL_PHOTO),
            (("LAUDO", "REPORT"), AssetClassification.REPORT),
        )
        for needles, classification in rules:
            if any(needle in value for needle in needles):
                return classification
        if Path(filename).suffix.casefold() in {".pdf", ".txt", ".doc", ".docx"}:
            return AssetClassification.AUXILIARY_DOCUMENT
        return AssetClassification.OTHER

    def download_digital_model_archive(
        self, item: CfazDigitalModelFile, destination: str | Path
    ) -> tuple[str, int, str]:
        """Baixa um ZIP de modelo sem persistir a URL assinada."""
        target = Path(destination)
        result = self._download_asset(
            AcquisitionAsset(
                asset_id=f"digital-model-{item.digital_model_id}-{item.stl_file_id}",
                download_url=item.download_url,
                filename=item.filename,
                classification=AssetClassification.DIGITAL_MODEL,
                source_field=item.source_field,
                provider_exam_id=item.digital_model_id,
                provider_asset_id=item.stl_file_id,
            ),
            target,
        )
        if result is None:
            raise CfazRequestError(
                "O download do modelo digital retornou uma página em vez do arquivo."
            )
        digest, size, mime_type = result
        if mime_type not in {
            "application/zip", "application/x-zip-compressed",
        } or not zipfile.is_zipfile(target):
            target.unlink(missing_ok=True)
            raise CfazRequestError(
                "O download do modelo digital não retornou um ZIP válido."
            )
        return digest, size, mime_type

    def finalize(self, request: AcquisitionRequest, *, success: bool) -> None:
        # A API/e-mail permanecem somente leitura; o histórico local controla idempotência.
        return None

    def _api_get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.API_ROOT}{path}"
        request_kwargs: dict[str, Any] = {
            "headers": dict(self._auth_headers), "timeout": self.timeout,
        }
        if params:
            request_kwargs["params"] = dict(params)
        try:
            response = self._session.get(url, **request_kwargs)
        except requests.Timeout:
            raise CfazRequestError("Tempo limite excedido ao consultar o Cfaz.") from None
        except requests.RequestException:
            raise CfazRequestError("Falha de comunicação ao consultar o Cfaz.") from None
        initial_response = response
        if self.auth_diagnostics:
            self.output(f"Consulta URL: {url}")
            self.output(
                "Cabeçalho Authorization presente: "
                f"{'sim' if self._authorization_present() else 'não'}"
            )
            self.output(
                f"Cookies enviados: {'sim' if self._session_has_cookies() else 'não'}"
            )
            if self._api_token:
                self.output(f"Tentativa 1: header Token -> HTTP {response.status_code}")
            if response.status_code == 401:
                self._diagnose_unauthorized(response)

        if response.status_code == 401 and self._api_token:
            try:
                response = self._session.get(
                    url,
                    headers={},
                    params={**(params or {}), "access_token": self._api_token},
                    timeout=self.timeout,
                )
            except requests.Timeout:
                raise CfazRequestError(
                    "Tempo limite excedido no fallback de autenticação do Cfaz."
                ) from None
            except requests.RequestException:
                raise CfazRequestError(
                    "Falha de comunicação no fallback de autenticação do Cfaz."
                ) from None
            if self.auth_diagnostics:
                # A URL permanece deliberadamente sem query string.
                self.output(
                    f"Tentativa 2: query access_token -> HTTP {response.status_code}"
                )

        if self.auth_diagnostics:
            self.output(f"Consulta HTTP Status: {response.status_code}")
            if response.status_code == 401 and response is not initial_response:
                self._diagnose_unauthorized(response)
        if self.payload_diagnostics and response.status_code in {401, 403}:
            self.output(
                f"Acesso ao pedido/arquivos: negado (HTTP {response.status_code})"
            )
        if not 200 <= response.status_code < 300:
            raise CfazHTTPError(response.status_code)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise CfazRequestError("O Cfaz retornou JSON inválido.") from None
        return payload

    def _page_get(self, path: str) -> Any:
        """Lê o JSON usado pela página sem registrar query ou conteúdo sensível."""
        url = path if str(path).startswith("https://") else f"{self.BASE_URL}{path}"
        headers = {
            **dict(self._auth_headers),
            "Accept": "application/json",
        }
        try:
            response = self._session.get(
                url, headers=headers, timeout=self.timeout
            )
        except requests.Timeout:
            raise CfazRequestError(
                "Tempo limite excedido ao consultar os modelos digitais."
            ) from None
        except requests.RequestException:
            raise CfazRequestError(
                "Falha de comunicação ao consultar os modelos digitais."
            ) from None
        if response.status_code in {401, 403} and self._api_token:
            try:
                response = self._session.get(
                    url,
                    headers={"Accept": "application/json"},
                    params={"access_token": self._api_token},
                    timeout=self.timeout,
                )
            except requests.Timeout:
                raise CfazRequestError(
                    "Tempo limite excedido no acesso autenticado aos modelos digitais."
                ) from None
            except requests.RequestException:
                raise CfazRequestError(
                    "Falha de comunicação no acesso autenticado aos modelos digitais."
                ) from None
        if (
            response.status_code in {401, 403}
            and self._email
            and self._password
        ):
            self._authenticate_with_credentials()
            try:
                response = self._session.get(
                    url,
                    headers={
                        **dict(self._auth_headers),
                        "Accept": "application/json",
                    },
                    timeout=self.timeout,
                )
            except requests.Timeout:
                raise CfazRequestError(
                    "Tempo limite excedido na sessão dos modelos digitais."
                ) from None
            except requests.RequestException:
                raise CfazRequestError(
                    "Falha de comunicação na sessão dos modelos digitais."
                ) from None
        # A API pode responder 204 ao token fixo para páginas protegidas.  A
        # aplicação web, porém, entrega os links assinados no HTML da página
        # autenticada.  Tente a representação de página (e, quando houver
        # credenciais configuradas, a sessão de login) sem registrar seu corpo.
        if response.status_code == 204:
            try:
                response = self._session.get(
                    url,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    timeout=self.timeout,
                )
            except requests.Timeout:
                raise CfazRequestError(
                    "Tempo limite excedido ao consultar a página dos modelos digitais."
                ) from None
            except requests.RequestException:
                raise CfazRequestError(
                    "Falha de comunicação ao consultar a página dos modelos digitais."
                ) from None
        if (
            response.status_code == 204
            and self._email
            and self._password
        ):
            self._authenticate_with_credentials()
            try:
                response = self._session.get(
                    url,
                    headers={
                        **dict(self._auth_headers),
                        "Accept": "text/html,application/xhtml+xml,application/json",
                    },
                    timeout=self.timeout,
                )
            except requests.Timeout:
                raise CfazRequestError(
                    "Tempo limite excedido na sessão da página dos modelos digitais."
                ) from None
            except requests.RequestException:
                raise CfazRequestError(
                    "Falha de comunicação na sessão da página dos modelos digitais."
                ) from None
        if not 200 <= response.status_code < 300:
            raise CfazRequestError(
                "O Cfaz recusou a consulta dos modelos digitais "
                f"(HTTP {response.status_code})."
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            # A página autenticada pode ser HTML com JSON/URLs assinadas
            # embutidos no componente DigitalModel. O corpo nunca é logado;
            # somente sua estrutura é analisada pelo extrator sanitizado.
            payload = getattr(response, "text", "") or ""
            if not isinstance(payload, str):
                payload = ""
        if not isinstance(payload, (dict, list, str)):
            raise CfazRequestError(
                "A página do Cfaz retornou uma estrutura inválida para os modelos digitais."
            )
        if self.payload_diagnostics:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
            size = response.headers.get("Content-Length") or len(getattr(response, "content", b"") or b"")
            if isinstance(payload, dict):
                keys = sorted(str(key) for key in payload)
                stl_count = sum(
                    len(value) for key, value in payload.items()
                    if str(key).casefold() in {"stl_files", "files"} and isinstance(value, list)
                )
            else:
                keys = []
                stl_count = 0
            candidates = self._digital_model_url_candidates(payload)
            self.output(
                f"Modelo endpoint {urlsplit(str(path)).path}: HTTP {response.status_code}; "
                f"Content-Type={content_type or 'não informado'}; tamanho={size}; "
                f"chaves={','.join(keys[:30]) or 'nenhuma'}; stl_files={stl_count}; "
                f"URLs candidatas={len(candidates)}"
            )
        return payload

    @classmethod
    def _digital_model_url_candidates(
        cls, payload: Any
    ) -> list[dict[str, str | None]]:
        candidates: list[dict[str, str | None]] = []
        seen: set[str] = set()
        expected_id_keys = (
            "stl_file_id", "stlfile_id", "file_id", "attachment_id", "id",
        )
        filename_keys = (
            "document_file_name", "filename", "file_name", "name",
        )

        def context_value(
            contexts: tuple[dict[str, Any], ...], keys: tuple[str, ...]
        ) -> str | None:
            for context in reversed(contexts):
                normalized = {
                    str(key).casefold(): value for key, value in context.items()
                }
                for key in keys:
                    value = cls._optional_identifier(normalized.get(key))
                    if value:
                        return value
            return None

        def strings(value: str) -> list[str]:
            decoded = unescape(
                value.replace("\\/", "/")
                .replace("\\u0026", "&")
                .replace("\\u003d", "=")
            )
            if decoded.strip().startswith(("https://", "http://")):
                return [decoded.strip()]
            return re.findall(r"https?://[^\s\"'<>]+", decoded)

        def visit(
            value: Any,
            contexts: tuple[dict[str, Any], ...] = (),
            path: tuple[str, ...] = (),
        ) -> None:
            if isinstance(value, dict):
                next_contexts = contexts + (value,)
                for key, child in value.items():
                    visit(child, next_contexts, path + (str(key),))
                return
            if isinstance(value, list):
                for position, child in enumerate(value):
                    visit(child, contexts, path + (f"[{position}]",))
                return
            if not isinstance(value, str):
                return
            for url in strings(value):
                url = url.rstrip("),;")
                if url in seen or not cls._is_digital_model_download_url(url):
                    continue
                cls._validate_download_url(url)
                seen.add(url)
                stl_file_id = context_value(contexts, expected_id_keys)
                filename = context_value(contexts, filename_keys)
                if not stl_file_id:
                    joined = ".".join(path)
                    match = re.search(r"(?<!\d)(\d{4,})(?!\d)", joined)
                    stl_file_id = match.group(1) if match else None
                candidates.append({
                    "url": url,
                    "stl_file_id": stl_file_id,
                    "filename": filename,
                })

        visit(payload)
        return candidates

    @classmethod
    def _assign_digital_model_urls(
        cls,
        descriptors: list[dict[str, Any]],
        candidates: list[dict[str, str | None]],
    ) -> None:
        expected = {
            str(item["stl_file_id"]): item for item in descriptors
        }
        used_urls: set[str] = set()
        for candidate in candidates:
            stl_file_id = str(candidate.get("stl_file_id") or "")
            item = expected.get(stl_file_id)
            if item is None or item.get("download_url"):
                continue
            url = str(candidate.get("url") or "")
            if not url:
                continue
            item["download_url"] = url
            item["filename"] = (
                cls._optional_text(candidate.get("filename"))
                or item.get("filename")
            )
            used_urls.add(url)
        # Nunca associe candidatos sem identidade pelo comprimento ou ordem.
        # A ausência de stl_file_id mantém o fluxo fechado.

    @classmethod
    def _is_digital_model_download_url(cls, value: str) -> bool:
        try:
            parts = urlsplit(value)
        except ValueError:
            return False
        host = (parts.hostname or "").casefold()
        if parts.scheme != "https" or not any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in cls.ALLOWED_DOWNLOAD_SUFFIXES
        ):
            return False
        suffix = Path(unquote(parts.path)).suffix.casefold()
        query_keys = {
            key.casefold() for key, _ in parse_qsl(
                parts.query, keep_blank_values=True
            )
        }
        signed = bool(query_keys.intersection({
            "signature", "googleaccessid", "expires",
            "x-goog-signature", "x-goog-credential",
        }))
        return (
            suffix in {".zip", ".stl"}
            or signed
            or host == "storage.googleapis.com"
            or host.endswith(".storage.googleapis.com")
        )

    @staticmethod
    def _optional_https_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text if text.startswith("https://") else None

    def _diagnose_login_response(self, response: Any) -> None:
        self.output(f"Login URL: {self.API_ROOT}/auth/sign_in")
        self.output(f"Login HTTP Status: {response.status_code}")
        self.output(
            "Login Content-Type: "
            f"{str(response.headers.get('Content-Type') or 'não informado').split(';', 1)[0]}"
        )
        token_received = bool(
            response.headers.get("access-token")
            or response.headers.get("Authorization")
        )
        cookie_received = bool(
            response.headers.get("Set-Cookie")
            or getattr(response, "cookies", None)
        )
        self.output(f"Token recebido: {'sim' if token_received else 'não'}")
        self.output(f"Cookie recebido: {'sim' if cookie_received else 'não'}")

    def _diagnose_configured_auth(self) -> None:
        authorization = str(self._auth_headers.get("Authorization") or "")
        if authorization.casefold().startswith("bearer "):
            auth_type = "Bearer"
        elif authorization.casefold().startswith("token "):
            auth_type = "Token"
        elif self._session_has_cookies() and not self._auth_headers:
            auth_type = "Cookie"
        elif any(key in self._auth_headers for key in ("access-token", "client", "uid")):
            auth_type = "Session"
        elif self._auth_headers:
            auth_type = "Outro"
        else:
            auth_type = "Cookie" if self._session_has_cookies() else "Outro"
        self.output(
            "Authorization configurado: "
            f"{'sim' if self._authorization_present() else 'não'}"
        )
        self.output(f"Tipo de autenticação: {auth_type}")
        if self._api_token:
            fingerprint = hashlib.sha256(self._api_token.encode("utf-8")).hexdigest()[:8]
            self.output("Authorization scheme: Token")
            self.output(f"Authorization token length: {len(self._api_token)}")
            self.output(f"Authorization token fingerprint: {fingerprint}")
            self.output(
                "Token sem aspas, espaços, quebra de linha ou prefixo embutido: sim"
            )

    @staticmethod
    def _validate_fixed_api_token(token: str) -> None:
        if (
            not token
            or any(character in token for character in ('"', "'", " ", "\r", "\n", "\t"))
        ):
            raise CfazAuthenticationError(
                "CFAZ_API_TOKEN possui formato inválido; informe somente o token bruto."
            )

    def _authorization_present(self) -> bool:
        return bool(str(self._auth_headers.get("Authorization") or "").strip())

    def _session_has_cookies(self) -> bool:
        try:
            return bool(getattr(self._session, "cookies", None))
        except Exception:
            return False

    def _diagnose_unauthorized(self, response: Any) -> None:
        try:
            payload = response.json()
        except (ValueError, TypeError, AttributeError):
            payload = {"message": "resposta 401 sem JSON válido"}
        sanitized = self._sanitize_auth_payload(payload)
        encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)[:4000]
        self.output(f"Resposta JSON 401 (sanitizada): {encoded}")
        if isinstance(sanitized, dict):
            message = sanitized.get("message") or sanitized.get("error")
            code = sanitized.get("code") or sanitized.get("error_code")
            self.output(f"Mensagem da API: {message or 'não informada'}")
            self.output(f"Código interno da API: {code or 'não informado'}")

    @classmethod
    def _sanitize_auth_payload(cls, value: Any) -> Any:
        sensitive = ("token", "cookie", "password", "secret", "authorization", "credential")
        if isinstance(value, dict):
            return {
                str(key): (
                    "[REDACTED]" if any(term in str(key).casefold() for term in sensitive)
                    else cls._sanitize_auth_payload(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize_auth_payload(item) for item in value[:50]]
        if isinstance(value, str):
            sanitized = re.sub(
                r"(?i)(bearer|token)\s+[A-Za-z0-9._~+/=-]+",
                r"\1 [REDACTED]", value,
            )
            return sanitized[:1000]
        return value

    def _assets(self, payload: dict[str, Any]) -> list[AcquisitionAsset]:
        found: list[tuple[str, dict[str, Any], str, AssetClassification, bool]] = []

        def urls(value: Any):
            if isinstance(value, str) and value.strip().startswith("https://"):
                yield value.strip()
            elif isinstance(value, dict):
                for child in value.values():
                    yield from urls(child)
            elif isinstance(value, list):
                for child in value:
                    yield from urls(child)

        for position, url in enumerate(urls(payload.get("images_download_links")), 1):
            found.append((
                url, {}, f"request.images_download_links[{position}]",
                AssetClassification.IMAGE, False,
            ))
        for report_position, report in enumerate(payload.get("reports") or [], 1):
            if not isinstance(report, dict):
                continue
            for position, url in enumerate(
                urls(report.get("associated_images_download_links")), 1
            ):
                found.append((
                    url, report,
                    f"request.reports[{report_position}].associated_images_download_links[{position}]",
                    AssetClassification.REPORT_ASSOCIATED_IMAGE, False,
                ))
            link = report.get("link")
            if isinstance(link, str) and link.strip().startswith("https://"):
                found.append((
                    link.strip(), report, f"request.reports[{report_position}].link",
                    AssetClassification.REPORT, True,
                ))

        # Modelos digitais são uma coleção explícita do payload e não devem
        # passar pelo walker genérico de imagens/documentos.
        for model_position, model in enumerate(payload.get("digital_models") or [], 1):
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip() or None
            for file_position, file in enumerate(model.get("stl_files") or [], 1):
                if not isinstance(file, dict):
                    continue
                url = file.get("download_url")
                if not isinstance(url, str) or not url.strip().startswith("https://"):
                    continue
                filename = str(file.get("document_file_name") or "").strip() or None
                found.append((
                    url.strip(), {
                        "filename": filename, "id": file.get("id"),
                        "provider_exam_id": model_id,
                    }, f"digital_models[{model_position}].stl_files[{file_position}]",
                    AssetClassification.DIGITAL_MODEL, False,
                ))

        def visit(value: Any, context: dict[str, Any] | None = None, path="request") -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = key.casefold()
                    is_file_field = any(term in normalized_key for term in (
                        "file", "url", "download", "image", "document",
                        "attachment", "result",
                    ))
                    if normalized_key == "link":
                        continue
                    if (
                        is_file_field and isinstance(child, str)
                        and child.strip().startswith("https://")
                    ):
                        found.append((
                            child.strip(), value, f"{path}.{key}",
                            self.classify(self._filename_from(value, child) or "", value),
                            False,
                        ))
                    elif is_file_field or isinstance(child, (dict, list)):
                        visit(child, value, f"{path}.{key}")
            elif isinstance(value, list):
                for position, child in enumerate(value, 1):
                    if isinstance(child, str) and child.startswith("https://"):
                        found.append((
                            child, context or {}, f"{path}[{position}]",
                            self.classify(self._filename_from(context or {}, child) or "", context),
                            False,
                        ))
                    else:
                        visit(child, context, f"{path}[{position}]")

        visit(payload)
        assets: list[AcquisitionAsset] = []
        seen_urls: set[str] = set()
        for position, (url, metadata, source_field, classification, probe_html) in enumerate(found, 1):
            self._validate_download_url(url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            filename = self._filename_from(metadata, url)
            asset_id = hashlib.sha256(source_field.encode("utf-8")).hexdigest()[:20]
            assets.append(AcquisitionAsset(
                asset_id=f"{asset_id}-{position}", download_url=url,
                filename=filename,
                classification=classification,
                source_field=source_field,
                probe_html=probe_html,
                provider_exam_id=str(metadata.get("provider_exam_id") or "") or None,
                provider_asset_id=str(metadata.get("id") or "") or None,
            ))
        return assets

    EXAM_COLLECTION_KEYS = frozenset({
        "carpals", "dental_models", "digital_models", "frontal_facials",
        "frontals", "implants", "lateral_facials", "panoramics", "reports",
        "teleradiographies", "tomographies",
    })
    FILE_FIELD_TERMS = (
        "file", "url", "download", "image", "document", "attachment", "result",
    )
    FILE_COLLECTION_KEYS = frozenset({"archives", "photos", "images_download_links"})

    @classmethod
    def _exam_count(cls, payload: dict[str, Any]) -> int:
        return sum(
            len(value) for key, value in payload.items()
            if key.casefold() in cls.EXAM_COLLECTION_KEYS and isinstance(value, list)
        )

    def _diagnose_payload_structure(
        self, response_payload: dict[str, Any], request_payload: dict[str, Any]
    ) -> None:
        self.output("Diagnóstico estrutural do payload Cfaz:")
        self.output(
            "Chaves de primeiro nível: "
            + ", ".join(sorted(str(key) for key in response_payload))
        )
        for key in sorted(response_payload):
            self.output(
                f"Campo raiz {key}: {self._safe_type_summary(response_payload[key])}"
            )
        exam_count = self._exam_count(request_payload)
        self.output(f"Quantidade de exames: {exam_count}")
        empty_file_collections = 0
        for collection_name in sorted(request_payload):
            value = request_payload[collection_name]
            if not isinstance(value, list):
                continue
            normalized = collection_name.casefold()
            relevant_collection = (
                normalized in self.EXAM_COLLECTION_KEYS
                or normalized in self.FILE_COLLECTION_KEYS
                or any(term in normalized for term in self.FILE_FIELD_TERMS)
            )
            if relevant_collection and not value:
                empty_file_collections += 1
                self.output(f"Coleção {collection_name}: list, quantidade=0")
            if normalized not in self.EXAM_COLLECTION_KEYS:
                continue
            for position, exam in enumerate(value, 1):
                if not isinstance(exam, dict):
                    self.output(
                        f"Exame {collection_name}[{position}]: {type(exam).__name__}"
                    )
                    continue
                self.output(
                    f"Exame {collection_name}[{position}] chaves: "
                    + ", ".join(sorted(str(key) for key in exam))
                )
        related_fields = list(self._related_payload_fields(request_payload))[:200]
        known_file_fields = {
            "download_url", "document_url", "file_url", "image", "document",
            "attachment", "result", "images_download_links",
            "associated_images_download_links",
        }
        unknown_file_fields = 0
        for path, field, child in related_fields:
            domains = sorted(self._url_domains(child))
            self.output(
                f"Campo relacionado {path}: {self._safe_type_summary(child)}; "
                f"URL={'sim' if domains else 'não'}; "
                f"domínio={','.join(domains) if domains else 'nenhum'}"
            )
            if field.casefold() not in known_file_fields:
                unknown_file_fields += 1
        self.output(
            "Coleções de arquivo vazias: " + str(empty_file_collections)
        )
        self.output(
            "Campos relacionados a arquivo ainda desconhecidos: "
            + str(unknown_file_fields)
        )
        self.output(
            "Endpoint separado por exam_id: não documentado publicamente; não consultado"
        )

    @staticmethod
    def _safe_type_summary(value: Any) -> str:
        if isinstance(value, (list, dict)):
            return f"{type(value).__name__}, quantidade={len(value)}"
        if value is None:
            return "null"
        return type(value).__name__

    @classmethod
    def _url_domains(cls, value: Any) -> set[str]:
        domains: set[str] = set()
        if isinstance(value, str):
            parts = urlsplit(value.strip())
            if parts.scheme in {"http", "https"} and parts.hostname:
                domains.add(parts.hostname.casefold())
        elif isinstance(value, dict):
            for child in value.values():
                domains.update(cls._url_domains(child))
        elif isinstance(value, list):
            for child in value:
                domains.update(cls._url_domains(child))
        return domains

    @classmethod
    def _related_payload_fields(
        cls, value: Any, path: str = "pedido"
    ):
        if isinstance(value, dict):
            for key, child in value.items():
                field = str(key)
                child_path = f"{path}.{field}"
                if any(
                    term in field.casefold()
                    for term in cls.FILE_FIELD_TERMS + ("link",)
                ):
                    yield child_path, field, child
                if isinstance(child, (dict, list)):
                    yield from cls._related_payload_fields(child, child_path)
        elif isinstance(value, list):
            for position, child in enumerate(value, 1):
                if isinstance(child, (dict, list)):
                    yield from cls._related_payload_fields(
                        child, f"{path}[{position}]"
                    )

    def _download_asset(
        self, asset: AcquisitionAsset, destination: Path
    ) -> tuple[str, int, str] | None:
        self._validate_download_url(asset.download_url)
        part = destination.with_suffix(destination.suffix + ".part")
        if destination.exists():
            raise CfazRequestError("Arquivo parcial conflitante exige revisão.")
        if part.exists():
            part.unlink()
        try:
            response = self._session.get(
                asset.download_url, headers=dict(self._auth_headers),
                timeout=self.timeout, stream=True, allow_redirects=False,
            )
        except requests.Timeout:
            raise CfazRequestError("Tempo limite excedido em um download Cfaz.") from None
        except requests.RequestException:
            raise CfazRequestError("Falha de comunicação em um download Cfaz.") from None
        if response.status_code != 200 and asset.probe_html:
            return None
        if not 200 <= response.status_code < 300:
            raise CfazRequestError(
                f"Download Cfaz rejeitado (HTTP {response.status_code})."
            )
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].casefold()
        try:
            content_length = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > self.max_file_bytes:
            raise CfazRequestError("Arquivo Cfaz excede o limite configurado.")
        digest = hashlib.sha256()
        size = 0
        detected_mime = content_type or "application/octet-stream"
        html_view = False
        try:
            with part.open("xb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise CfazRequestError("Arquivo Cfaz excede o limite configurado.")
                    if size == len(chunk):
                        detected_mime = self._detect_mime(chunk, content_type)
                        if detected_mime == "text/html":
                            if asset.probe_html:
                                html_view = True
                                break
                            raise CfazRequestError(
                                "O Cfaz retornou HTML em vez de um arquivo permitido."
                            )
                        if not self._allowed_file_content(
                            detected_mime, destination, probe=asset.probe_html
                        ):
                            if asset.probe_html:
                                html_view = True
                                break
                            raise CfazRequestError(
                                "O Cfaz retornou conteúdo incompatível com um arquivo permitido."
                            )
                    output.write(chunk)
                    digest.update(chunk)
            if html_view:
                part.unlink(missing_ok=True)
                return None
            if size == 0:
                raise CfazRequestError("O Cfaz retornou um arquivo vazio.")
            part.replace(destination)
        except CfazRequestError:
            part.unlink(missing_ok=True)
            raise
        except requests.Timeout:
            part.unlink(missing_ok=True)
            raise CfazRequestError("Tempo limite excedido em um download Cfaz.") from None
        except requests.RequestException:
            part.unlink(missing_ok=True)
            raise CfazRequestError("Falha de comunicação em um download Cfaz.") from None
        except OSError:
            part.unlink(missing_ok=True)
            raise CfazRequestError("Não foi possível gravar o arquivo na quarentena.") from None
        except Exception:
            part.unlink(missing_ok=True)
            raise CfazRequestError("O download Cfaz foi interrompido com segurança.") from None
        return digest.hexdigest(), size, detected_mime

    @staticmethod
    def _detect_mime(content: bytes, response_type: str) -> str:
        prefix = content[:512].lstrip().lower()
        if response_type in {"text/html", "application/xhtml+xml"} or prefix.startswith(
            (b"<!doctype html", b"<html", b"<head", b"<body")
        ):
            return "text/html"
        signatures = (
            (b"\xff\xd8\xff", "image/jpeg"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
            (b"%PDF-", "application/pdf"),
            (b"PK\x03\x04", "application/zip"),
        )
        for signature, mime_type in signatures:
            if content.startswith(signature):
                return mime_type
        return response_type or "application/octet-stream"

    @staticmethod
    def _allowed_file_content(mime_type: str, path: Path, *, probe: bool) -> bool:
        allowed_exact = {
            "application/pdf", "application/zip", "application/x-zip-compressed",
            "application/x-rar-compressed", "application/dicom",
            "application/octet-stream", "text/plain",
        }
        if mime_type.startswith("image/") or mime_type in allowed_exact:
            if mime_type != "application/octet-stream" or not probe:
                return True
            return path.suffix.casefold() in {
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".zip",
                ".rar", ".dcm", ".txt", ".doc", ".docx",
            }
        return False

    def _refine_classification(
        self, asset: AcquisitionAsset, mime_type: str, path: Path
    ) -> AssetClassification:
        if asset.classification == AssetClassification.REPORT_ASSOCIATED_IMAGE:
            return AssetClassification.REPORT_ASSOCIATED_IMAGE
        if mime_type == "application/pdf":
            return AssetClassification.REPORT if asset.probe_html else AssetClassification.AUXILIARY_DOCUMENT
        refined = self.classify(path.name, {"type": mime_type})
        if refined != AssetClassification.OTHER:
            return refined
        if mime_type.startswith("image/"):
            return AssetClassification.IMAGE
        return asset.classification

    @staticmethod
    def _classification_from_state(record: Any) -> AssetClassification | None:
        if not isinstance(record, dict):
            return None
        try:
            return AssetClassification(record.get("classification"))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _request_id_from_notification(cls, notification: Any) -> str:
        ids = cls.notification_request_ids(notification)
        if len(ids) != 1:
            raise CfazRequestError("A notificação Cfaz não identifica um único pedido.")
        return next(iter(ids))

    @classmethod
    def _notification_content(cls, notification: Any) -> str:
        text = "\n".join(str(value) for value in (
            getattr(notification, "subject", ""),
            getattr(notification, "text_body", ""),
            getattr(notification, "html_body", ""),
        ) if value)
        # Links de trackers costumam carregar a URL Cfaz HTML-escaped e/ou
        # percent-encoded como parâmetro. Duas passagens cobrem esse formato
        # sem seguir o redirecionamento nem expor a URL.
        text = unescape(text)
        return unquote(unquote(text))

    @classmethod
    def notification_request_ids(cls, notification: Any) -> set[str]:
        return set(cls.REQUEST_URL.findall(cls._notification_content(notification)))

    @classmethod
    def notification_has_request_link(cls, notification: Any) -> bool:
        return bool(cls.notification_request_ids(notification))

    notification_request_id = _request_id_from_notification

    @classmethod
    def notification_patient_name(cls, notification: Any) -> str | None:
        text = "\n".join(str(value) for value in (
            getattr(notification, "subject", ""),
            getattr(notification, "text_body", ""),
        ) if value)
        match = re.search(
            r"(?:paciente|patient)\s*[:\-]\s*([^\n|;]{2,120})",
            text, re.IGNORECASE,
        )
        if not match:
            match = re.match(
                r"\s*cfazpost\s*-\s*([^\n|;]{2,120})",
                str(getattr(notification, "subject", "")), re.IGNORECASE,
            )
        return cls._optional_text(match.group(1)) if match else None

    @classmethod
    def _validate_download_url(cls, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        if parts.scheme != "https" or parts.username or parts.password or not any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in cls.ALLOWED_DOWNLOAD_SUFFIXES
        ):
            raise CfazRequestError("O pedido Cfaz contém uma URL de download não autorizada.")

    @staticmethod
    def _provider_exam_id(payload: dict[str, Any]) -> str | None:
        values = []
        for key in ("panoramics", "teleradiographies", "tomographies", "reports"):
            for item in payload.get(key) or []:
                if isinstance(item, dict) and item.get("id") is not None:
                    values.append(f"{key}:{item['id']}")
        return ",".join(sorted(values)) or None

    @classmethod
    def _clinic_number(cls, payload: dict[str, Any]) -> str | None:
        for key in ("clinic_number", "clinic_sequential_id"):
            value = cls._optional_identifier(payload.get(key))
            if value:
                return value
        clinic = payload.get("clinic")
        if isinstance(clinic, dict):
            for key in ("sequential_id", "number", "clinic_number"):
                value = cls._optional_identifier(clinic.get(key))
                if value:
                    return value
        return cls._optional_identifier(payload.get("clinic_id"))

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if isinstance(value, bool) or value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _filename_from(metadata: dict[str, Any], url: str) -> str | None:
        for key in ("filename", "file_name", "name", "original_filename"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = Path(unquote(urlsplit(url).path)).name
        return value or None

    @classmethod
    def _file_mime(cls, path: Path) -> str:
        try:
            with path.open("rb") as stream:
                return cls._detect_mime(stream.read(512), "")
        except OSError:
            raise CfazRequestError("Não foi possível identificar o arquivo adquirido.") from None

    @staticmethod
    def _asset_collection(asset: AcquisitionAsset) -> str:
        source = asset.source_field or ""
        if ".associated_images_download_links" in source:
            return "reports.associated_images_download_links"
        if source.startswith("request.images_download_links"):
            return "request.images_download_links"
        if source.endswith(".link"):
            return "reports.link"
        return source.rsplit("[", 1)[0] or "unknown"

    @classmethod
    def _stored_filename(
        cls, asset: AcquisitionAsset, mime_type: str,
        counters: dict[str, int], used: set[str],
    ) -> str:
        if asset.classification == AssetClassification.REPORT_ASSOCIATED_IMAGE:
            prefix = "laudo_imagem"
        elif asset.classification in {
            AssetClassification.PANORAMIC, AssetClassification.TELERADIOGRAPHY,
            AssetClassification.PERIAPICAL_SERIES, AssetClassification.BITE_WING,
        }:
            prefix = "radiografia"
        elif asset.classification == AssetClassification.CLINICAL_PHOTO:
            prefix = "fotografia"
        elif asset.classification == AssetClassification.REPORT:
            prefix = "laudo"
        elif asset.classification == AssetClassification.DIGITAL_MODEL:
            prefix = "modelo_digital"
        else:
            prefix = "imagem" if mime_type.startswith("image/") else "arquivo"
        extension = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "application/pdf": ".pdf", "application/zip": ".zip",
            "application/x-zip-compressed": ".zip",
            "application/x-rar-compressed": ".rar", "application/dicom": ".dcm",
            "text/plain": ".txt",
        }.get(mime_type, Path(asset.filename or "").suffix.casefold() or ".bin")
        while True:
            counters[prefix] = counters.get(prefix, 0) + 1
            candidate = f"{prefix}_{counters[prefix]:03d}{extension}"
            if candidate.casefold() not in used:
                used.add(candidate.casefold())
                return candidate

    @staticmethod
    def _image_dimensions(path: Path, mime_type: str) -> tuple[int | None, int | None]:
        try:
            data = path.read_bytes()[:1024 * 1024]
        except OSError:
            return None, None
        if mime_type == "image/png" and len(data) >= 24 and data.startswith(b"\x89PNG"):
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if mime_type != "image/jpeg" or not data.startswith(b"\xff\xd8"):
            return None, None
        offset = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                height = int.from_bytes(data[offset + 3:offset + 5], "big")
                width = int.from_bytes(data[offset + 5:offset + 7], "big")
                return width or None, height or None
            offset += length
        return None, None

    @staticmethod
    def _without_redundant_thumbnails(downloaded):
        originals = [
            item[4] for item in downloaded
            if item[4]["collection"] == "request.images_download_links"
            and item[4]["width"] and item[4]["height"]
            and max(item[4]["width"], item[4]["height"]) > 640
        ]
        if not originals:
            return downloaded
        kept = []
        for item in downloaded:
            metadata = item[4]
            if not (
                metadata["is_thumbnail"]
                and metadata["collection"] == "reports.associated_images_download_links"
                and metadata["width"] and metadata["height"]
            ):
                kept.append(item)
                continue
            ratio = metadata["width"] / metadata["height"]
            matching_original = any(
                abs((original["width"] / original["height"]) - ratio) <= 0.03
                for original in originals
            )
            if not matching_original:
                kept.append(item)
        return kept

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(value.replace("\\", "/")).name.strip()
        name = re.sub(r"[^\w.() -]+", "_", name, flags=re.UNICODE).strip(" .")
        if not name or name in {".", ".."}:
            raise CfazRequestError("O Cfaz retornou um nome de arquivo inválido.")
        return name[:180]

    def _unique_filename(self, asset, position: int, used: set[str]) -> str:
        original = asset.filename or f"arquivo-{position}.bin"
        safe = self._safe_filename(original)
        candidate = safe
        if candidate.casefold() in used:
            candidate = f"{Path(safe).stem}-{asset.asset_id[:8]}{Path(safe).suffix}"
        if candidate.casefold() in used:
            raise CfazRequestError("O pedido possui nomes de arquivo conflitantes.")
        used.add(candidate.casefold())
        return candidate

    @staticmethod
    def _archive_name(request: AcquisitionRequest) -> str:
        patient = re.sub(r"[^\w -]+", "", request.patient_name or "PACIENTE").strip()
        stamp = request.exam_date.strftime("%Y%m%d%H%M%S") if request.exam_date else ""
        return f"{patient}_{stamp}_CFAZ-{request.request_id}.zip"

    @staticmethod
    def _write_deterministic_zip(archive: Path, files) -> None:
        temporary = archive.with_suffix(".zip.part")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for asset, path, *_ in sorted(files, key=lambda item: item[1].name.casefold()):
                prefix = (
                    "fotografias/" if asset.classification == AssetClassification.CLINICAL_PHOTO
                    else ""
                )
                info = zipfile.ZipInfo(prefix + path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                with path.open("rb") as source, output.open(info, "w") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        temporary.replace(archive)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _read_state(cls, path: Path, request_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text("utf-8"))
            return payload.get("files", {}) if payload.get("request_id") == request_id else {}
        except (OSError, ValueError, AttributeError):
            return {}

    @staticmethod
    def _write_state(path: Path, request_id: str, files: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"request_id": request_id, "files": files}, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def _completed_file(cls, path: Path, record: Any) -> bool:
        return bool(
            isinstance(record, dict) and path.is_file()
            and path.stat().st_size == record.get("size")
            and cls._sha256(path) == record.get("sha256")
        )

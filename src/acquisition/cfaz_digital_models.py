"""Aquisição suplementar e idempotente de modelos digitais Cfaz."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Callable
import unicodedata
import zipfile

from acquisition.cfaz_provider import (
    CfazDigitalModelFile,
    CfazDigitalModelInventory,
    CfazProvider,
)
from acquisition.cfaz_stl_download import (
    CfazStlDownloadError,
    download_stl_diagnostic,
)
from acquisition.clinical_normalizer import (
    CLINICAL_FOLDERS,
    ClinicalAssetNormalizer,
    ClinicalCategory,
)
from acquisition.models.clinical_package import (
    ClinicalAsset as ContractClinicalAsset,
)
from acquisition.cfaz_metadata_transaction import (
    LocalMetadataCoordinator,
    ProductiveMetadataSagaFactory,
    SupplementModelIdentity,
)
from integrations.onedrive_graph import GraphRollbackJournal


class CfazDigitalModelError(RuntimeError):
    """Erro sanitizado do fluxo suplementar de modelos digitais."""


PRODUCTIVE_METADATA_SAGA_TYPE: type = ProductiveMetadataSagaFactory


def validate_metadata_coordinator(
    coordinator: Any, *, allow_test_double: bool = False
) -> Any:
    """Valida localmente o gate transacional, sem inicializar dependências."""
    saga_complete = (
        getattr(coordinator, "CAPABILITY", None)
        == LocalMetadataCoordinator.CAPABILITY
        and callable(getattr(coordinator, "create", None))
    )
    productive = (
        type(coordinator) is PRODUCTIVE_METADATA_SAGA_TYPE
        and saga_complete
        and getattr(coordinator, "PRODUCTIVE_IMPLEMENTATION", False) is True
        and not getattr(coordinator, "TEST_DOUBLE", False)
    )
    legacy_test_double = (
        allow_test_double
        and getattr(coordinator, "TEST_DOUBLE", False) is True
        and getattr(coordinator, "CAPABILITY", None)
        == "cfaz-supplement-local-tx-v1"
        and all(
            callable(getattr(coordinator, name, None))
            for name in ("preflight", "commit", "rollback")
        )
    )
    if not productive and not legacy_test_double:
        raise CfazDigitalModelError(
            "Saga durável produtiva de manifesto, índice, "
            "histórico e intake não está disponível; aplicação bloqueada."
        )
    return coordinator


@dataclass(frozen=True)
class CfazDigitalModelsResult:
    request_id: str
    model_count: int
    provider_file_count: int
    pending_file_count: int
    added_file_count: int
    reused_file_count: int
    state: str
    manifest_path: Path
    onedrive_destination: str
    indexed: bool
    planned_model_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedModel:
    digital_model_id: str
    stl_file_id: str
    source_name: str
    local_source: Path
    stored_name: str
    relative_folder: str
    sha256: str
    size: int
    metadata: dict[str, Any]
    already_manifested: bool

    @property
    def relative_path(self) -> str:
        return f"{self.relative_folder}/{self.stored_name}"


@dataclass(frozen=True)
class _PlannedModel:
    item: CfazDigitalModelFile
    stored_name: str
    relative_folder: str

    @property
    def relative_path(self) -> str:
        return f"{self.relative_folder}/{self.stored_name}"


class CfazDigitalModelSupplement:
    """Acrescenta STL a um pedido COMPLETE sem reimportar os demais assets."""

    MARKER_VERSION = 1
    MAX_ZIP_ENTRIES = 100

    def __init__(
        self,
        *,
        provider,
        history,
        staging_root: str | Path,
        graph=None,
        exam_index_service=None,
        metadata_coordinator=None,
        event_recorder: Callable[[str], None] | None = None,
        allow_test_metadata_coordinator: bool = False,
        max_file_bytes: int = 2 * 1024 * 1024 * 1024,
        output: Callable[[str], None] = print,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.history = history
        self.staging_root = Path(staging_root).expanduser().resolve()
        self.graph = graph
        self.exam_index_service = exam_index_service
        self.metadata_coordinator = metadata_coordinator
        self.event_recorder = event_recorder or (lambda _event: None)
        self.allow_test_metadata_coordinator = allow_test_metadata_coordinator
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.output = output
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def run(
        self, identifier: str, *, apply: bool = False
    ) -> CfazDigitalModelsResult:
        value = str(identifier or "").strip()
        if not value.isdigit():
            raise CfazDigitalModelError("O Request ID do Cfaz é inválido.")
        record = self.history.get_record(value)
        if record is None or record.status != "COMPLETE":
            raise CfazDigitalModelError(
                "Histórico Cfaz COMPLETE não encontrado de forma única."
            )
        manifest_path = self._find_manifest(record)
        manifest = self._read_json(manifest_path)
        publication = manifest.get("publication")
        if (
            not isinstance(publication, dict)
            or str(publication.get("state") or "").upper() != "COMPLETE"
        ):
            raise CfazDigitalModelError(
                "O manifesto local do pedido não está COMPLETE."
            )
        destination = str(
            record.onedrive_destination
            or manifest.get("onedrive_destination")
            or ""
        ).strip()
        if not destination:
            raise CfazDigitalModelError(
                "Destino OneDrive não registrado para o pedido."
            )
        marker = manifest.get("cfaz_digital_models")
        if self._completed_marker_is_valid(marker, manifest_path.parent):
            count = len(marker.get("provider_files") or {})
            if apply and not self.allow_test_metadata_coordinator:
                self._validate_local_transaction_capability()
                self._run_metadata_saga(
                    record=record,
                    updated_manifest=manifest,
                    manifest_path=manifest_path,
                )
            return CfazDigitalModelsResult(
                request_id=record.provider_request_id or record.request_id,
                model_count=int(marker.get("digital_models") or 0),
                provider_file_count=count,
                pending_file_count=0,
                added_file_count=0,
                reused_file_count=count,
                state="ALREADY_COMPLETE",
                manifest_path=manifest_path,
                onedrive_destination=destination,
                indexed=True,
            )
        if apply:
            self._validate_local_transaction_capability()

        lookup_id = str(record.provider_request_id or value)
        self.event_recorder("resolve_models")
        inventory = self.provider.discover_digital_models(lookup_id)
        self._validate_inventory(record, inventory)
        existing_ids = self._manifest_stl_ids(manifest)
        if existing_ids and not self._completed_marker_is_valid(
            marker, manifest_path.parent
        ):
            raise CfazDigitalModelError(
                "Há modelos digitais em estado parcial; revisão obrigatória."
            )
        pending = [
            item for item in inventory.files
            if item.stl_file_id not in existing_ids
        ]
        self.output(f"Pedido: {record.sequential_id or value}")
        self.output(f"ID interno: {inventory.request.request_id}")
        self.output(f"digital_models: {inventory.model_count}")
        self.output(f"stl_files: {len(inventory.files)}")
        self.output(f"STL já incorporados: {len(inventory.files) - len(pending)}")
        self.output(f"STL pendentes: {len(pending)}")
        for item in inventory.files:
            status = (
                "já incorporado"
                if item.stl_file_id in existing_ids
                else "pendente"
            )
            self.output(
                f"Modelo {item.digital_model_id}; STL {item.stl_file_id}; "
                f"status={status}."
            )
        if self.graph is None:
            raise CfazDigitalModelError(
                "Cliente OneDrive obrigatório para o preflight remoto."
            )
        if apply and not inventory.files:
            return CfazDigitalModelsResult(
                request_id=inventory.request.request_id,
                model_count=inventory.model_count,
                provider_file_count=0,
                pending_file_count=0,
                added_file_count=0,
                reused_file_count=0,
                state="NO_MODELS",
                manifest_path=manifest_path,
                onedrive_destination=destination,
                indexed=False,
            )
        self._validate_model_preflight(inventory)
        if apply:
            self._validate_apply_graph_capability()
        plans = self._plan_destinations(
            inventory, manifest=manifest, destination=manifest_path.parent
        )
        self.event_recorder("local_preflight")
        if apply and self.allow_test_metadata_coordinator:
            self.metadata_coordinator.preflight(
                record=record,
                manifest=manifest,
                planned_assets=plans,
            )
        self.event_recorder("remote_preflight")
        remote_state = self._preflight_remote(
            destination=destination,
            manifest=manifest,
            plans=plans,
        )
        if not apply:
            return CfazDigitalModelsResult(
                request_id=inventory.request.request_id,
                model_count=inventory.model_count,
                provider_file_count=len(inventory.files),
                pending_file_count=len(pending),
                added_file_count=0,
                reused_file_count=len(inventory.files) - len(pending),
                state="DRY_RUN",
                manifest_path=manifest_path,
                onedrive_destination=destination,
                indexed=False,
                planned_model_paths=tuple(
                    plan.relative_path for plan in plans
                ),
            )
        return self._apply(
            record=record,
            inventory=inventory,
            manifest_path=manifest_path,
            manifest=manifest,
            destination=destination,
            plans=plans,
            remote_state=remote_state,
        )

    def _apply(
        self,
        *,
        record,
        inventory: CfazDigitalModelInventory,
        manifest_path: Path,
        manifest: dict[str, Any],
        destination: str,
        plans: tuple[_PlannedModel, ...],
        remote_state: tuple[Any, Any, dict[str, Any]],
    ) -> CfazDigitalModelsResult:
        original_bytes = manifest_path.read_bytes()
        working = json.loads(json.dumps(manifest))
        if (
            not self.staging_root.is_dir()
            or self.staging_root.is_symlink()
        ):
            raise CfazDigitalModelError(
                "Staging root inexistente ou inseguro."
            )
        try:
            work_root = Path(tempfile.mkdtemp(
                prefix=".cfaz-models-transaction-",
                dir=self.staging_root,
            )).resolve()
        except OSError:
            raise CfazDigitalModelError(
                "Não foi possível adquirir staging transacional exclusivo."
            ) from None
        owned_work_root = (
            work_root.parent == self.staging_root
            and not work_root.is_symlink()
            and not any(work_root.iterdir())
        )
        if not owned_work_root:
            raise CfazDigitalModelError(
                "O staging transacional não nasceu vazio e exclusivo."
            )
        self.event_recorder("staging_acquired")
        prepared: list[_PreparedModel] = []
        created_local: list[Path] = []
        remote_manifest_started = False
        remote_folder, model_folder, remote_manifest = remote_state
        rollback_journal = GraphRollbackJournal()
        try:
            used_paths = self._manifest_relative_paths(working)
            plan_by_id = {plan.item.stl_file_id: plan for plan in plans}
            for item in inventory.files:
                existing = self._prepared_from_manifest(
                    item, working, manifest_path.parent
                )
                if existing is not None:
                    prepared.append(existing)
                    used_paths.add(existing.relative_path.casefold())
                    continue
                prepared.append(
                    self._download_and_prepare(
                        item,
                        inventory=inventory,
                        work_root=work_root,
                        used_paths=used_paths,
                        planned=plan_by_id[item.stl_file_id],
                    )
                )
            self.event_recorder("downloads_validated")
            hashes: dict[str, str] = {}
            for plan in prepared:
                prior_identity = hashes.get(plan.sha256)
                if (
                    prior_identity is not None
                    and prior_identity != plan.stl_file_id
                ):
                    raise CfazDigitalModelError(
                        "Identidades STL distintas produziram conteúdo idêntico; "
                        "revisão obrigatória."
                    )
                hashes[plan.sha256] = plan.stl_file_id
            now = self._utc_now()
            working = self._merge_manifest(
                working, prepared, inventory=inventory, timestamp=now
            )
            transaction_manifest = work_root / "manifest.pending.json"
            self._write_json(transaction_manifest, working)
            self.graph.upload_small_file(
                remote_folder, transaction_manifest, remote_filename="manifest.json"
            )
            remote_manifest_started = True
            for plan in prepared:
                publication = working["publication"]
                marker = working["cfaz_digital_models"]
                marker_file = marker["provider_files"][plan.stl_file_id]
                uploaded = publication["uploaded_files"]
                if not plan.already_manifested:
                    marker_file["remote_upload_intended"] = True
                    self._write_json(transaction_manifest, working)
                    self.graph.upload_small_file(
                        remote_folder,
                        transaction_manifest,
                        remote_filename="manifest.json",
                    )
                    created_reference = self.graph.upload_small_file_transactional(
                        model_folder,
                        plan.local_source,
                        remote_filename=plan.stored_name,
                    )
                    rollback_journal.record(created_reference)
                uploaded[plan.relative_path] = {
                    "sha256": plan.sha256,
                    "size": plan.size,
                }
                marker_file["remote_uploaded"] = True
                marker_file["remote_upload_intended"] = False
                marker_file["uploaded_at"] = now
                publication["uploaded_files_count"] = len(uploaded)
                publication["uploaded_bytes"] = sum(
                    int(item.get("size") or 0)
                    for item in uploaded.values()
                    if isinstance(item, dict)
                )
                self._write_json(transaction_manifest, working)
                self.graph.upload_small_file(
                    remote_folder, transaction_manifest, remote_filename="manifest.json"
                )

            final = json.loads(json.dumps(working))
            final_marker = final["cfaz_digital_models"]
            final_marker["state"] = "COMPLETE"
            final_marker["completed_at"] = now
            final["status"] = "COMPLETED"
            final["publication"]["state"] = "COMPLETE"
            final_path = work_root / "manifest.final.json"
            self._write_json(final_path, final)
            self.graph.upload_small_file(
                remote_folder, final_path, remote_filename="manifest.json"
            )
            self.event_recorder("remote_persisted")
            for plan in prepared:
                if plan.already_manifested:
                    continue
                target = manifest_path.parent / plan.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise CfazDigitalModelError(
                        "Destino local passou a estar ocupado após o preflight."
                    )
                try:
                    self._promote_local(plan.local_source, target)
                except OSError:
                    target.unlink(missing_ok=True)
                    raise CfazDigitalModelError(
                        "Não foi possível incorporar o STL ao staging local."
                    ) from None
                if self._sha256(target) != plan.sha256:
                    target.unlink(missing_ok=True)
                    raise CfazDigitalModelError(
                        "A validação local do STL incorporado falhou."
                    )
                created_local.append(target)
            self.event_recorder("local_promotion")
            if self.allow_test_metadata_coordinator:
                operation_id = hashlib.sha256(
                    (
                        str(inventory.request.request_id)
                        + "|"
                        + "|".join(sorted(
                            item.stl_file_id for item in inventory.files
                        ))
                    ).encode("utf-8")
                ).hexdigest()
                self.metadata_coordinator.commit(
                    operation_id=operation_id,
                    record=record,
                    original_manifest=manifest,
                    updated_manifest=final,
                    manifest_path=manifest_path,
                    created_files=tuple(created_local),
                )
            else:
                self._run_metadata_saga(
                    record=record,
                    updated_manifest=final,
                    manifest_path=manifest_path,
                )
            self.event_recorder("metadata_committed")
        except Exception as exc:
            rollback_ok = True
            _, rollback_failures = rollback_journal.rollback_all(self.graph)
            if rollback_failures:
                rollback_ok = False
            if remote_manifest_started and remote_folder is not None:
                rollback_path = work_root / "manifest.rollback.json"
                try:
                    rollback_path.write_bytes(original_bytes)
                    self.graph.upload_small_file(
                        remote_folder,
                        rollback_path,
                        remote_filename="manifest.json",
                    )
                except Exception:
                    rollback_ok = False
            for path in reversed(created_local):
                path.unlink(missing_ok=True)
            self._remove_empty_model_folder(manifest_path.parent)
            if self.allow_test_metadata_coordinator:
                try:
                    self.metadata_coordinator.rollback()
                except Exception:
                    rollback_ok = False
            if isinstance(exc, CfazDigitalModelError) and rollback_ok:
                self._cleanup_work_root(work_root, owned=owned_work_root)
                raise
            self._cleanup_work_root(work_root, owned=owned_work_root)
            raise CfazDigitalModelError(
                "A incorporação dos modelos digitais foi interrompida; "
                + (
                    "o estado seguro foi preservado para retomada."
                    if rollback_ok
                    else "o rollback remoto ficou incompleto e exige revisão."
                )
            ) from exc

        indexed = True
        added = sum(not item.already_manifested for item in prepared)
        self._cleanup_work_root(work_root, owned=owned_work_root)
        return CfazDigitalModelsResult(
            request_id=inventory.request.request_id,
            model_count=inventory.model_count,
            provider_file_count=len(inventory.files),
            pending_file_count=added,
            added_file_count=added,
            reused_file_count=len(prepared) - added,
            state="COMPLETE",
            manifest_path=manifest_path,
            onedrive_destination=destination,
            indexed=indexed,
        )

    @staticmethod
    def _promote_local(source_path: Path, target: Path) -> None:
        with source_path.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)

    @staticmethod
    def _cleanup_work_root(work_root: Path, *, owned: bool) -> None:
        if (
            not owned
            or work_root.is_symlink()
            or not work_root.name.startswith(".cfaz-models-transaction-")
        ):
            return
        shutil.rmtree(work_root, ignore_errors=True)

    def _validate_local_transaction_capability(self) -> None:
        validate_metadata_coordinator(
            self.metadata_coordinator,
            allow_test_double=self.allow_test_metadata_coordinator,
        )

    @staticmethod
    def _technical_models(
        updated_manifest: dict[str, Any],
    ) -> tuple[SupplementModelIdentity, ...]:
        marker = updated_manifest.get("cfaz_digital_models")
        files = marker.get("provider_files") if isinstance(marker, dict) else None
        if not isinstance(files, dict) or len(files) != 2:
            raise CfazDigitalModelError(
                "Marcador operacional não contém exatamente dois modelos."
            )
        models: list[SupplementModelIdentity] = []
        for position, file_id in enumerate(sorted(files), start=1):
            value = files[file_id]
            if not isinstance(value, dict):
                raise CfazDigitalModelError(
                    "Marcador operacional de modelo é inválido."
                )
            models.append(SupplementModelIdentity(
                semantic_role=f"cfaz-model-{position:02d}",
                stl_file_id=str(value.get("stl_file_id") or file_id),
                sha256=str(value.get("sha256") or ""),
                destination=str(value.get("relative_path") or ""),
            ))
        return tuple(models)

    def _run_metadata_saga(
        self, *, record: Any, updated_manifest: dict[str, Any],
        manifest_path: Path,
    ) -> Any:
        models = self._technical_models(updated_manifest)
        saga = self.metadata_coordinator.create(
            record=record,
            updated_manifest=updated_manifest,
            manifest_path=manifest_path,
            models=models,
        )
        if (
            not isinstance(saga, LocalMetadataCoordinator)
            or saga.models != models
        ):
            raise CfazDigitalModelError(
                "Fábrica produtiva não criou a saga durável esperada."
            )
        existing = saga.operations.get(saga.operation_id)
        return saga.resume() if existing is not None else saga.execute()

    def _plan_destinations(
        self,
        inventory: CfazDigitalModelInventory,
        *,
        manifest: dict[str, Any],
        destination: Path,
    ) -> tuple[_PlannedModel, ...]:
        used_paths = self._manifest_relative_paths(manifest)
        plans: list[_PlannedModel] = []
        folder = CLINICAL_FOLDERS[ClinicalCategory.DIGITAL_MODEL]
        for item in inventory.files:
            declared_stem = Path(str(item.filename or "")).stem
            source_name = (
                f"{declared_stem}.stl" if declared_stem else "modelo.stl"
            )
            stored_name = self._next_stored_name(
                source_name=source_name,
                model_name=item.model_name,
                used_paths=used_paths,
            )
            plan = _PlannedModel(item, stored_name, folder)
            target = destination / plan.relative_path
            if target.exists():
                raise CfazDigitalModelError(
                    "Destino local de modelo já está ocupado; revisão obrigatória."
                )
            used_paths.add(plan.relative_path.casefold())
            plans.append(plan)
        if (
            len(plans) != 2
            or len({plan.relative_path.casefold() for plan in plans}) != 2
        ):
            raise CfazDigitalModelError(
                "Os dois destinos finais não são distintos e inequívocos."
            )
        return tuple(plans)

    def _preflight_remote(
        self,
        *,
        destination: str,
        manifest: dict[str, Any],
        plans: tuple[_PlannedModel, ...],
    ) -> tuple[Any, Any, dict[str, Any]]:
        try:
            remote_folder = self._resolve_remote_folder(destination)
            remote_manifest = self.graph.download_json_file(
                remote_folder, "manifest.json"
            )
            self._validate_remote_manifest(manifest, remote_manifest)
            expected_name = CLINICAL_FOLDERS[
                ClinicalCategory.DIGITAL_MODEL
            ]
            folders = [
                item for item in self.graph.list_children(remote_folder)
                if isinstance(item, dict)
                and isinstance(item.get("folder"), dict)
                and self._normalize_name(str(item.get("name") or ""))
                == self._normalize_name(expected_name)
            ]
            if len(folders) != 1:
                raise CfazDigitalModelError(
                    "Pasta remota de modelos ausente ou ambígua."
                )
            model_folder = self.graph.folder_from_child_item(
                remote_folder, folders[0]
            )
            children = self.graph.list_children(model_folder)
        except CfazDigitalModelError:
            raise
        except Exception:
            raise CfazDigitalModelError(
                "Preflight remoto falhou antes dos downloads."
            ) from None
        occupied = {
            str(item.get("name") or "").casefold()
            for item in children
            if isinstance(item, dict)
        }
        if any(plan.stored_name.casefold() in occupied for plan in plans):
            raise CfazDigitalModelError(
                "Destino remoto de modelo já está ocupado; revisão obrigatória."
            )
        self.event_recorder("remote_preflight_complete")
        return remote_folder, model_folder, remote_manifest

    def _download_and_prepare(
        self,
        item: CfazDigitalModelFile,
        *,
        inventory: CfazDigitalModelInventory,
        work_root: Path,
        used_paths: set[str],
        planned: _PlannedModel,
    ) -> _PreparedModel:
        prepared_root = work_root / f"prepared-{hashlib.sha256(item.stl_file_id.encode()).hexdigest()[:16]}"
        prepared_root.mkdir(parents=True, exist_ok=False)
        source = prepared_root / "model.stl"
        try:
            with download_stl_diagnostic(
                item.download_url,
                session=getattr(self.provider, "_session", None),
                max_file_bytes=self.max_file_bytes,
            ) as diagnostic:
                with diagnostic.validated_path.open("rb") as input_stream, source.open("xb") as output:
                    shutil.copyfileobj(input_stream, output, 1024 * 1024)
                digest = diagnostic.sha256
        except (CfazStlDownloadError, OSError) as exc:
            shutil.rmtree(prepared_root, ignore_errors=True)
            raise CfazDigitalModelError(
                "O download ou a validação estrutural do STL falhou."
            ) from exc
        if self._sha256(source) != digest:
            shutil.rmtree(prepared_root, ignore_errors=True)
            raise CfazDigitalModelError(
                "O fingerprint do STL preparado é inconsistente."
            )
        source_name = (
            f"{Path(str(item.filename or '')).stem}.stl"
            if Path(str(item.filename or "")).stem else "modelo.stl"
        )
        stored_name = planned.stored_name
        folder = planned.relative_folder
        relative_path = f"{folder}/{stored_name}"
        used_paths.add(relative_path.casefold())
        timestamp = self._utc_now()
        metadata = {
            "asset_id": (
                f"cfaz-digital-model-{item.digital_model_id}-"
                f"{item.stl_file_id}"
            ),
            "provider": "cfaz",
            "provider_request_id": (
                inventory.request.provider_request_id
                or inventory.request.request_id
            ),
            "sequential_id": inventory.request.sequential_id,
            "original_source": item.source_field,
            "source_collection": "digital_models.stl_files",
            "provider_section": item.source_field,
            "provider_display_name": "Modelo digital",
            "source_name": source_name,
            "source_url_hash": None,
            "download_source": "cfaz_authenticated_request_page",
            "stored_name": stored_name,
            "normalized_filename": stored_name,
            "relative_folder": folder,
            "detected_mime": "model/stl",
            "mime_type": "model/stl",
            "detected_extension": ".stl",
            "extension": ".stl",
            "asset_type": "DIGITAL_MODEL",
            "clinical_category": "DIGITAL_MODEL",
            "width": None,
            "height": None,
            "size_bytes": source.stat().st_size,
            "size": source.stat().st_size,
            "sha256": digest,
            "collection": "digital_models.stl_files",
            "provider_exam_id": item.digital_model_id,
            "provider_asset_id": item.stl_file_id,
            "digital_model_id": item.digital_model_id,
            "stl_file_id": item.stl_file_id,
            "provider_metadata": {
                "provider": "cfaz",
                "provider_request_id": (
                    inventory.request.provider_request_id
                    or inventory.request.request_id
                ),
                "digital_model_id": item.digital_model_id,
                "stl_file_id": item.stl_file_id,
            },
            "is_thumbnail": False,
            "thumbnail": False,
            "is_duplicate": False,
            "duplicate_of": None,
            "downloaded_at": timestamp,
            "normalized_at": timestamp,
            "normalization_action": "SUPPLEMENTED",
        }
        return _PreparedModel(
            digital_model_id=item.digital_model_id,
            stl_file_id=item.stl_file_id,
            source_name=source_name,
            local_source=source,
            stored_name=stored_name,
            relative_folder=folder,
            sha256=digest,
            size=source.stat().st_size,
            metadata=metadata,
            already_manifested=False,
        )

    def _validate_model_preflight(
        self, inventory: CfazDigitalModelInventory
    ) -> None:
        if len(inventory.files) != 2:
            raise CfazDigitalModelError(
                "A aplicação exige exatamente dois descritores STL."
            )
        identities = [str(item.stl_file_id or "") for item in inventory.files]
        urls = [str(item.download_url or "").strip() for item in inventory.files]
        if (
            any(not value for value in identities)
            or len(set(identities)) != len(identities)
            or any(not value for value in urls)
            or len(set(urls)) != len(urls)
        ):
            raise CfazDigitalModelError(
                "Mapa STL incompleto, duplicado ou ambíguo; revisão obrigatória."
            )
        for url in urls:
            CfazProvider._validate_download_url(url)

    def _validate_apply_graph_capability(self) -> None:
        capability = getattr(
            self.graph, "VERIFIED_COMPENSATION_CAPABILITY", None
        )
        if self.graph is not None and (
            capability != "graph-item-id-etag-delete-v1"
            or not callable(
                getattr(self.graph, "upload_small_file_transactional", None)
            )
            or not callable(
                getattr(self.graph, "delete_created_item_verified", None)
            )
        ):
            raise CfazDigitalModelError(
                "O cliente remoto não oferece rollback verificável; "
                "aplicação bloqueada."
            )

    def _validate_apply_preflight(
        self, inventory: CfazDigitalModelInventory
    ) -> None:
        self._validate_model_preflight(inventory)
        self._validate_apply_graph_capability()

    def _prepared_from_manifest(
        self,
        item: CfazDigitalModelFile,
        manifest: dict[str, Any],
        destination: Path,
    ) -> _PreparedModel | None:
        for asset in self._all_asset_mappings(manifest):
            stl_file_id = str(
                asset.get("stl_file_id")
                or asset.get("provider_asset_id")
                or ""
            )
            if stl_file_id != item.stl_file_id:
                continue
            stored = str(
                asset.get("stored_name")
                or asset.get("normalized_filename")
                or ""
            )
            folder = str(asset.get("relative_folder") or "")
            digest = str(asset.get("sha256") or "")
            relative = f"{folder}/{stored}" if folder else stored
            local = destination / relative
            if (
                not stored
                or not digest
                or not local.is_file()
                or self._sha256(local) != digest
            ):
                raise CfazDigitalModelError(
                    "O manifesto referencia um STL local ausente ou inválido."
                )
            return _PreparedModel(
                digital_model_id=item.digital_model_id,
                stl_file_id=item.stl_file_id,
                source_name=str(
                    asset.get("source_name")
                    or asset.get("original_filename")
                    or stored
                ),
                local_source=local,
                stored_name=stored,
                relative_folder=folder,
                sha256=digest,
                size=local.stat().st_size,
                metadata=self._supplement_metadata(asset, item),
                already_manifested=True,
            )
        return None

    def _merge_manifest(
        self,
        manifest: dict[str, Any],
        prepared: list[_PreparedModel],
        *,
        inventory: CfazDigitalModelInventory,
        timestamp: str,
    ) -> dict[str, Any]:
        acquisition = manifest.get("acquisition")
        if not isinstance(acquisition, dict):
            acquisition = {}
            manifest["acquisition"] = acquisition
        files = self._mapping_list(acquisition.get("files"))
        assets = self._mapping_list(acquisition.get("assets"))
        package = acquisition.get("clinical_package")
        if not isinstance(package, dict):
            package = {}
            acquisition["clinical_package"] = package
        package_assets = self._mapping_list(package.get("assets"))
        top_assets = (
            self._mapping_list(manifest.get("assets"))
            if isinstance(manifest.get("assets"), list)
            else None
        )
        top_package = manifest.get("clinical_package")
        top_package_assets = (
            self._mapping_list(top_package.get("assets"))
            if isinstance(top_package, dict)
            and isinstance(top_package.get("assets"), list)
            else None
        )
        checksums = manifest.get("checksums")
        if not isinstance(checksums, dict):
            checksums = {}
            manifest["checksums"] = checksums
        publication = manifest.get("publication")
        if not isinstance(publication, dict):
            raise CfazDigitalModelError(
                "Manifesto sem inventário de publicação."
            )
        uploaded = publication.get("uploaded_files")
        if not isinstance(uploaded, dict):
            uploaded = {}
            publication["uploaded_files"] = uploaded
        prior_marker = manifest.get("cfaz_digital_models")
        prior_marker = prior_marker if isinstance(prior_marker, dict) else {}
        provider_files = dict(prior_marker.get("provider_files") or {})
        if prior_marker:
            base_total_files = int(
                (
                    prior_marker.get("base_total_files")
                    if "base_total_files" in prior_marker
                    else publication.get("total_files")
                )
                or 0
            )
            base_total_bytes = int(
                (
                    prior_marker.get("base_total_bytes")
                    if "base_total_bytes" in prior_marker
                    else publication.get("total_bytes")
                )
                or 0
            )
        else:
            existing = {
                item.stl_file_id: item
                for item in prepared if item.already_manifested
            }
            base_total_files = max(
                0,
                int(publication.get("total_files") or 0) - len(existing),
            )
            base_total_bytes = max(
                0,
                int(publication.get("total_bytes") or 0)
                - sum(item.size for item in existing.values()),
            )
        for plan in prepared:
            if not self._contains_stl_id(files, plan.stl_file_id):
                files.append(dict(plan.metadata))
            if not self._contains_stl_id(assets, plan.stl_file_id):
                assets.append(dict(plan.metadata))
            contract = ContractClinicalAsset.from_mapping(
                dict(plan.metadata), provider="cfaz"
            ).to_dict()
            if not self._contains_stl_id(package_assets, plan.stl_file_id):
                package_assets.append(contract)
            if top_assets is not None and not self._contains_stl_id(
                top_assets, plan.stl_file_id
            ):
                top_assets.append(dict(plan.metadata))
            if (
                top_package_assets is not None
                and not self._contains_stl_id(
                    top_package_assets, plan.stl_file_id
                )
            ):
                top_package_assets.append(contract)
            checksums[plan.relative_path] = plan.sha256
            prior = provider_files.get(plan.stl_file_id)
            provider_files[plan.stl_file_id] = {
                "digital_model_id": plan.digital_model_id,
                "stl_file_id": plan.stl_file_id,
                "source_name": plan.source_name,
                "stored_name": plan.stored_name,
                "relative_folder": plan.relative_folder,
                "relative_path": plan.relative_path,
                "sha256": plan.sha256,
                "size_bytes": plan.size,
                "remote_uploaded": bool(
                    isinstance(prior, dict) and prior.get("remote_uploaded")
                ),
                "remote_upload_intended": bool(
                    isinstance(prior, dict)
                    and prior.get("remote_upload_intended")
                ),
                "uploaded_at": (
                    prior.get("uploaded_at")
                    if isinstance(prior, dict) else None
                ),
            }
        acquisition["files"] = files
        acquisition["assets"] = assets
        acquisition["classifications"] = sorted({
            *(
                str(value) for value in acquisition.get("classifications", [])
                if value
            ),
            "Modelo digital",
        })
        acquisition["asset_count"] = len(files)
        package["assets"] = package_assets
        if top_assets is not None:
            manifest["assets"] = top_assets
        if top_package_assets is not None:
            top_package["assets"] = top_package_assets
        manifest["file_count"] = int(manifest.get("file_count") or 0) + sum(
            not item.already_manifested for item in prepared
        )
        manifest["total_size_bytes"] = int(
            manifest.get("total_size_bytes") or 0
        ) + sum(
            item.size for item in prepared if not item.already_manifested
        )
        publication["total_files"] = base_total_files + len(provider_files)
        publication["total_bytes"] = base_total_bytes + sum(
            int(item.get("size_bytes") or 0)
            for item in provider_files.values()
            if isinstance(item, dict)
        )
        publication["state"] = "COMPLETE"
        manifest["status"] = "COMPLETED"
        manifest["cfaz_digital_models"] = {
            "schema_version": self.MARKER_VERSION,
            "state": "IN_PROGRESS",
            "started_at": str(prior_marker.get("started_at") or timestamp),
            "completed_at": None,
            "digital_models": inventory.model_count,
            "stl_files": len(inventory.files),
            "base_total_files": base_total_files,
            "base_total_bytes": base_total_bytes,
            "provider_files": provider_files,
        }
        return manifest

    def _extract_single_stl(
        self, archive: Path, extraction_root: Path
    ) -> tuple[Path, str]:
        if extraction_root.exists():
            shutil.rmtree(extraction_root)
        extraction_root.mkdir(parents=True)
        extracted: list[tuple[Path, str]] = []
        try:
            with zipfile.ZipFile(archive) as source:
                entries = [
                    item for item in source.infolist() if not item.is_dir()
                ]
                if not entries or len(entries) > self.MAX_ZIP_ENTRIES:
                    raise CfazDigitalModelError(
                        "O ZIP do modelo possui quantidade inválida de arquivos."
                    )
                total = 0
                for position, entry in enumerate(entries, 1):
                    relative = PurePosixPath(entry.filename)
                    mode = (entry.external_attr >> 16) & 0o170000
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or "\\" in entry.filename
                        or mode == stat.S_IFLNK
                        or entry.flag_bits & 0x1
                    ):
                        raise CfazDigitalModelError(
                            "O ZIP do modelo contém uma entrada insegura."
                        )
                    total += int(entry.file_size)
                    if total > self.max_file_bytes:
                        raise CfazDigitalModelError(
                            "O conteúdo extraído do modelo excede o limite configurado."
                        )
                    target = extraction_root / f"entry-{position:03d}"
                    written = 0
                    with source.open(entry) as input_stream, target.open("xb") as output:
                        while chunk := input_stream.read(1024 * 1024):
                            written += len(chunk)
                            if written > entry.file_size or total > self.max_file_bytes:
                                raise CfazDigitalModelError(
                                    "O ZIP do modelo excedeu os limites seguros."
                                )
                            output.write(chunk)
                    detected = ClinicalAssetNormalizer.detect(
                        target, source_name=Path(entry.filename).name
                    )
                    if detected.mime == "model/stl":
                        extracted.append(
                            (target, Path(entry.filename).name or "modelo.stl")
                        )
                    else:
                        target.unlink(missing_ok=True)
        except CfazDigitalModelError:
            shutil.rmtree(extraction_root, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError):
            shutil.rmtree(extraction_root, ignore_errors=True)
            raise CfazDigitalModelError(
                "Não foi possível extrair o ZIP do modelo digital com segurança."
            ) from None
        if len(extracted) != 1:
            shutil.rmtree(extraction_root, ignore_errors=True)
            raise CfazDigitalModelError(
                "Cada registro STL deve produzir exatamente um arquivo STL válido."
            )
        return extracted[0]

    def _find_manifest(self, record) -> Path:
        matches = []
        expected = {
            str(record.request_id or ""),
            str(record.provider_request_id or ""),
            str(record.sequential_id or ""),
        } - {""}
        for path in self.staging_root.rglob("manifest.json"):
            try:
                manifest = self._read_json(path)
            except CfazDigitalModelError:
                continue
            acquisition = manifest.get("acquisition")
            if not isinstance(acquisition, dict):
                continue
            identifiers = {
                str(acquisition.get(key) or "")
                for key in (
                    "request_id", "provider_request_id", "sequential_id",
                )
            } - {""}
            if expected.intersection(identifiers):
                matches.append(path)
        if len(matches) != 1:
            raise CfazDigitalModelError(
                "Manifesto local do pedido não foi localizado de forma única."
            )
        return matches[0]

    def _resolve_remote_folder(self, destination: str):
        parts = [part.strip() for part in destination.split("/") if part.strip()]
        if not parts:
            raise CfazDigitalModelError("Destino OneDrive inválido.")
        current = self.graph.find_root_folder(parts[0])
        for expected in parts[1:]:
            normalized = self._normalize_name(expected)
            candidates = [
                item for item in self.graph.list_children(current)
                if isinstance(item.get("folder"), dict)
                and self._normalize_name(str(item.get("name") or ""))
                == normalized
            ]
            if len(candidates) != 1:
                raise CfazDigitalModelError(
                    "Destino remoto dos modelos não foi localizado de forma única."
                )
            current = self.graph.folder_from_child_item(
                current, candidates[0]
            )
        return current

    @staticmethod
    def _validate_remote_manifest(
        local: dict[str, Any], remote: Any
    ) -> None:
        local_publication = local.get("publication")
        remote_publication = (
            remote.get("publication") if isinstance(remote, dict) else None
        )
        if not isinstance(remote_publication, dict):
            raise CfazDigitalModelError(
                "Destino remoto sem manifesto confiável."
            )
        local_exam = str(
            (local_publication or {}).get("exam_id")
            or (local_publication or {}).get("source_archive_sha256")
            or ""
        )
        remote_exam = str(
            remote_publication.get("exam_id")
            or remote_publication.get("source_archive_sha256")
            or ""
        )
        if not local_exam or local_exam != remote_exam:
            raise CfazDigitalModelError(
                "O manifesto remoto pertence a outro exame."
            )

    @staticmethod
    def _validate_inventory(
        record, inventory: CfazDigitalModelInventory
    ) -> None:
        expected = {
            str(record.request_id or ""),
            str(record.provider_request_id or ""),
            str(record.sequential_id or ""),
        } - {""}
        actual = {
            str(inventory.request.request_id or ""),
            str(inventory.request.provider_request_id or ""),
            str(inventory.request.sequential_id or ""),
        } - {""}
        if not expected.intersection(actual):
            raise CfazDigitalModelError(
                "O Cfaz retornou modelos de um pedido diferente."
            )
        identifiers = [item.stl_file_id for item in inventory.files]
        if len(identifiers) != len(set(identifiers)):
            raise CfazDigitalModelError(
                "O Cfaz retornou identificadores STL duplicados."
            )

    @classmethod
    def _completed_marker_is_valid(
        cls, marker: Any, destination: Path
    ) -> bool:
        if (
            not isinstance(marker, dict)
            or str(marker.get("state") or "").upper() != "COMPLETE"
        ):
            return False
        files = marker.get("provider_files")
        if not isinstance(files, dict) or not files:
            return False
        for record in files.values():
            if not isinstance(record, dict):
                return False
            relative = str(record.get("relative_path") or "")
            digest = str(record.get("sha256") or "")
            path = destination / relative
            if (
                not relative
                or not digest
                or not path.is_file()
                or cls._sha256(path) != digest
                or not record.get("remote_uploaded")
            ):
                return False
        return True

    @classmethod
    def _manifest_stl_ids(cls, manifest: dict[str, Any]) -> set[str]:
        return {
            str(
                item.get("stl_file_id")
                or item.get("provider_asset_id")
                or ""
            )
            for item in cls._all_asset_mappings(manifest)
        } - {""}

    @classmethod
    def _manifest_relative_paths(
        cls, manifest: dict[str, Any]
    ) -> set[str]:
        values = set()
        checksums = manifest.get("checksums")
        if isinstance(checksums, dict):
            values.update(str(key).casefold() for key in checksums)
        for item in cls._all_asset_mappings(manifest):
            stored = str(
                item.get("stored_name")
                or item.get("normalized_filename")
                or ""
            )
            folder = str(item.get("relative_folder") or "")
            if stored:
                values.add(
                    (f"{folder}/{stored}" if folder else stored).casefold()
                )
        return values

    @staticmethod
    def _mapping_list(value: Any) -> list[dict[str, Any]]:
        return [
            dict(item) for item in value or [] if isinstance(item, dict)
        ] if isinstance(value, list) else []

    @classmethod
    def _all_asset_mappings(
        cls, manifest: dict[str, Any]
    ) -> list[dict[str, Any]]:
        acquisition = manifest.get("acquisition")
        acquisition = acquisition if isinstance(acquisition, dict) else {}
        package = acquisition.get("clinical_package")
        package = package if isinstance(package, dict) else {}
        top_package = manifest.get("clinical_package")
        top_package = top_package if isinstance(top_package, dict) else {}
        values = []
        for candidate in (
            acquisition.get("files"),
            acquisition.get("assets"),
            package.get("assets"),
            manifest.get("assets"),
            top_package.get("assets"),
        ):
            values.extend(cls._mapping_list(candidate))
        return values

    @staticmethod
    def _contains_stl_id(
        values: list[dict[str, Any]], stl_file_id: str
    ) -> bool:
        return any(
            str(
                item.get("stl_file_id")
                or item.get("provider_asset_id")
                or ""
            ) == stl_file_id
            for item in values
        )

    @staticmethod
    def _supplement_metadata(
        asset: dict[str, Any], item: CfazDigitalModelFile
    ) -> dict[str, Any]:
        value = dict(asset)
        value.setdefault("provider", "cfaz")
        value.setdefault("source_collection", "digital_models.stl_files")
        value.setdefault("collection", "digital_models.stl_files")
        value.setdefault("clinical_category", "DIGITAL_MODEL")
        value.setdefault("detected_mime", "model/stl")
        value.setdefault("mime_type", "model/stl")
        value.setdefault("detected_extension", ".stl")
        value.setdefault("extension", ".stl")
        value["provider_exam_id"] = item.digital_model_id
        value["provider_asset_id"] = item.stl_file_id
        value["digital_model_id"] = item.digital_model_id
        value["stl_file_id"] = item.stl_file_id
        return value

    @staticmethod
    def _next_stored_name(
        *, source_name: str, model_name: str | None,
        used_paths: set[str],
    ) -> str:
        value = CfazDigitalModelSupplement._normalize_name(
            f"{source_name} {model_name or ''}"
        )
        if any(term in value for term in ("LOWER", "MANDIB")):
            prefix = "modelo_mandibula"
        elif any(term in value for term in ("UPPER", "MAXIL")):
            prefix = "modelo_maxila"
        else:
            prefix = "modelo_digital"
        folder = CLINICAL_FOLDERS[ClinicalCategory.DIGITAL_MODEL]
        for position in range(1, 10_000):
            name = f"{prefix}_{position:03d}.stl"
            if f"{folder}/{name}".casefold() not in used_paths:
                return name
        raise CfazDigitalModelError(
            "Não foi possível gerar um nome determinístico para o STL."
        )

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        without_marks = "".join(
            character for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^A-Z0-9]+", " ", without_marks.upper()).strip()

    @staticmethod
    def _remove_empty_model_folder(destination: Path) -> None:
        folder = destination / CLINICAL_FOLDERS[ClinicalCategory.DIGITAL_MODEL]
        try:
            folder.rmdir()
        except OSError:
            pass

    def _utc_now(self) -> str:
        value = self.now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise CfazDigitalModelError("Manifesto local inválido.") from None
        if not isinstance(value, dict):
            raise CfazDigitalModelError("Manifesto local inválido.")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    value, ensure_ascii=False, indent=2, sort_keys=True
                ) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise CfazDigitalModelError(
                "Não foi possível atualizar o manifesto dos modelos."
            ) from None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

"""Reparo idempotente de nomes JPEG de aquisições Cfaz já concluídas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from acquisition.cfaz_operations import CfazHistoryRepository
from acquisition.clinical_normalizer import (
    CLINICAL_FOLDERS, ClinicalAssetNormalizer,
)
from radiology.patient_matcher import PatientMatcher


class CfazRepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class CfazRepairResult:
    request_id: str
    renamed_files: int
    jpeg_files: int
    duplicate_files: int
    repaired_at: str
    repair_version: int


class CfazCompletedImportRepair:
    REPAIR_VERSION = 16

    def __init__(
        self, *, history: CfazHistoryRepository, graph, staging_root: str | Path,
        output: Callable[[str], None] = print,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.history = history
        self.graph = graph
        self.staging_root = Path(staging_root).expanduser().resolve()
        self.output = output
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def repair(self, identifier: str, *, apply: bool = True) -> CfazRepairResult:
        value = str(identifier or "").strip()
        record = self.history.get_record(value)
        if record is None or record.status != "COMPLETE":
            raise CfazRepairError("Histórico Cfaz COMPLETE não encontrado de forma única.")
        manifest_path = self._find_manifest(record)
        manifest = self._read_json(manifest_path)
        prior_repair = manifest.get("cfaz_file_repair")
        if apply and (
            isinstance(prior_repair, dict)
            and prior_repair.get("repair_version") == self.REPAIR_VERSION
        ):
            repaired_at = str(prior_repair.get("repaired_at") or "")
            if not apply:
                self.output(
                    f"DRY-RUN: manifesto já possui repair_version={self.REPAIR_VERSION}; "
                    "nenhuma alteração necessária."
                )
                return CfazRepairResult(
                    request_id=record.provider_request_id or record.request_id,
                    renamed_files=0,
                    jpeg_files=len((manifest.get("acquisition") or {}).get("files") or []),
                    duplicate_files=int(prior_repair.get("duplicate_files") or 0),
                    repaired_at=repaired_at,
                    repair_version=self.REPAIR_VERSION,
                )
            destination = str(record.onedrive_destination or "").strip()
            if not destination:
                raise CfazRepairError("Destino OneDrive não registrado no histórico.")
            remote_folder = self._resolve_remote_folder(destination)
            # Reenviar o manifesto é seguro e permite retomar uma execução que
            # tenha sido interrompida depois da atualização local.
            self.graph.upload_small_file(
                remote_folder, manifest_path, remote_filename="manifest.json"
            )
            self.history.mark_repaired(value, self.REPAIR_VERSION)
            return CfazRepairResult(
                request_id=record.provider_request_id or record.request_id,
                renamed_files=0,
                jpeg_files=len((manifest.get("acquisition") or {}).get("files") or []),
                duplicate_files=int(prior_repair.get("duplicate_files") or 0),
                repaired_at=repaired_at,
                repair_version=self.REPAIR_VERSION,
            )

        checksums = manifest.get("checksums")
        publication = manifest.get("publication")
        uploaded = publication.get("uploaded_files") if isinstance(publication, dict) else None
        if not isinstance(checksums, dict) or not isinstance(uploaded, dict):
            raise CfazRepairError("Manifesto concluído sem inventário reparável.")

        existing_metadata = {
            str(item.get("stored_name") or ""): item
            for item in ((manifest.get("acquisition") or {}).get("files") or [])
            if isinstance(item, dict)
        }
        plans: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        duplicate_count = 0
        counters = {}
        for old_name in sorted(checksums, key=str.casefold):
            local_file = manifest_path.parent / old_name
            if not local_file.is_file():
                raise CfazRepairError("Arquivo local do manifesto não foi encontrado.")
            detected = ClinicalAssetNormalizer.detect(
                local_file, source_name=local_file.name
            )
            digest = self._sha256(local_file)
            if digest in seen_sha:
                duplicate_count += 1
                continue
            seen_sha.add(digest)
            prior = existing_metadata.get(old_name, {})
            collection = str(
                prior.get("source_collection")
                or prior.get("collection")
                or prior.get("original_source")
                or "legacy_repair"
            )
            category, subtype = ClinicalAssetNormalizer._category(
                collection, detected, local_file.name
            )
            relative_folder = CLINICAL_FOLDERS[category]
            if category.value == "REPORT" and "associated_images" in collection:
                relative_folder += "/Imagens associadas"
            stored_name = ClinicalAssetNormalizer._stored_name(
                category, subtype, detected.extension, counters
            )
            plans.append({
                "old_name": old_name,
                "stored_name": stored_name,
                "relative_folder": relative_folder,
                "path": local_file,
                "sha256": digest,
                "detected": detected,
                "collection": collection,
                "category": category.value,
                "is_thumbnail": bool(prior.get("is_thumbnail")),
            })
        if not plans:
            raise CfazRepairError("Nenhum arquivo local foi encontrado para reparo.")

        destination = str(record.onedrive_destination or "").strip()
        if not destination:
            raise CfazRepairError("Destino OneDrive não registrado no histórico.")
        if not apply:
            for plan in plans:
                reason = self._classification_reason(plan["collection"], plan["category"])
                confidence = "baixa" if plan["category"] == "DOCUMENTATION" else "média"
                dimensions = (
                    f"{plan['detected'].width}x{plan['detected'].height}"
                    if plan["detected"].width and plan["detected"].height
                    else "indisponível"
                )
                self.output(
                    "DRY-RUN: "
                    f"{plan['old_name']} -> {plan['stored_name']} | "
                    f"source_collection={plan['collection']} | "
                    f"clinical_category={plan['category']} | "
                    f"confiança={confidence} | motivo={reason} | dimensões={dimensions} | "
                    f"tipo={plan['detected'].mime} | "
                    f"destino={destination}/{plan['relative_folder']} | "
                    f"ação={'duplicata' if plan.get('is_duplicate') else 'normalizar'} | "
                    f"thumbnail={'sim' if plan['is_thumbnail'] else 'não'}"
                )
            return CfazRepairResult(
                request_id=record.provider_request_id or record.request_id,
                renamed_files=0,
                jpeg_files=len(plans),
                duplicate_files=duplicate_count,
                repaired_at="",
                repair_version=self.REPAIR_VERSION,
            )
        remote_folder = self._resolve_remote_folder(destination)
        remote_folders = {Path(): remote_folder}
        for plan in plans:
            relative_folder = Path(plan["relative_folder"])
            remote_parent = remote_folder
            accumulated = Path()
            for part in relative_folder.parts:
                accumulated /= part
                if accumulated not in remote_folders:
                    remote_folders[accumulated] = self.graph.ensure_folder(
                        remote_parent, part
                    )
                remote_parent = remote_folders[accumulated]
            self.graph.move_child_file(
                remote_folder, plan["old_name"],
                remote_parent, plan["stored_name"],
            )

        renamed = 0
        repaired_at = self.now_provider()
        if repaired_at.tzinfo is None:
            repaired_at = repaired_at.replace(tzinfo=timezone.utc)
        repaired_text = repaired_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        new_checksums = dict(checksums)
        new_uploaded = dict(uploaded)
        metadata = []
        for plan in plans:
            old_name = plan["old_name"]
            stored_name = plan["stored_name"]
            path = plan["path"]
            relative_folder = plan["relative_folder"]
            target = manifest_path.parent / relative_folder / stored_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if path != target:
                if target.exists() and self._sha256(target) != plan["sha256"]:
                    raise CfazRepairError("Nome local corrigido conflita com outro arquivo.")
                if not target.exists():
                    path.replace(target)
                renamed += 1
            checksum = new_checksums.pop(old_name, plan["sha256"])
            stored_relative = f"{relative_folder}/{stored_name}"
            new_checksums[stored_relative] = checksum
            upload_record = new_uploaded.pop(old_name, None)
            if upload_record is not None:
                new_uploaded[stored_relative] = upload_record
            detected = plan["detected"]
            metadata.append({
                "original_source": f"legacy_manifest:{old_name}",
                "source_collection": plan["collection"],
                "source_name": old_name,
                "source_url_hash": None,
                "stored_name": stored_name,
                "relative_folder": relative_folder,
                "detected_mime": detected.mime,
                "detected_extension": detected.extension,
                "extension": detected.extension,
                "asset_type": detected.asset_type,
                "clinical_category": plan["category"],
                "width": detected.width,
                "height": detected.height,
                "size_bytes": target.stat().st_size,
                "sha256": plan["sha256"],
                "collection": plan["collection"],
                "is_thumbnail": plan["is_thumbnail"],
                "is_duplicate": False,
                "duplicate_of": None,
                "normalized_at": repaired_text,
            })
        acquisition = manifest.get("acquisition")
        if not isinstance(acquisition, dict):
            acquisition = {}
            manifest["acquisition"] = acquisition
        acquisition["files"] = metadata
        acquisition["assets"] = metadata
        acquisition["normalizer_version"] = ClinicalAssetNormalizer.VERSION
        acquisition["normalized_at"] = repaired_text
        acquisition["repair_version"] = self.REPAIR_VERSION
        acquisition["repaired_at"] = repaired_text
        manifest["schema_version"] = "16.0"
        manifest["provider"] = "cfaz"
        manifest["request"] = {
            "provider_request_id": record.provider_request_id,
            "sequential_id": record.sequential_id,
            "clinic_number": record.clinic_number,
        }
        manifest["assets"] = metadata
        manifest["normalizer_version"] = ClinicalAssetNormalizer.VERSION
        manifest["normalized_at"] = repaired_text
        manifest["repair_version"] = self.REPAIR_VERSION
        manifest["repaired_at"] = repaired_text
        manifest["checksums"] = new_checksums
        publication["uploaded_files"] = new_uploaded
        manifest["cfaz_file_repair"] = {
            "repaired_at": repaired_text,
            "repair_version": self.REPAIR_VERSION,
            "duplicate_files": duplicate_count,
        }
        self._write_json(manifest_path, manifest)
        self.graph.upload_small_file(
            remote_folder, manifest_path, remote_filename="manifest.json"
        )
        self.history.mark_repaired(value, self.REPAIR_VERSION)
        return CfazRepairResult(
            request_id=record.provider_request_id or record.request_id,
            renamed_files=renamed,
            jpeg_files=len(metadata),
            duplicate_files=duplicate_count,
            repaired_at=repaired_text,
            repair_version=self.REPAIR_VERSION,
        )

    @staticmethod
    def _classification_reason(collection: str, category: str) -> str:
        value = str(collection or "").casefold()
        if "associated_images_download_links" in value:
            return "origem reports.associated_images_download_links"
        if any(term in value for term in ("frontal_facials", "lateral_facials", "fotograf")):
            return "coleção de fotografias clínicas"
        if any(term in value for term in ("frontals", "teleradiograph", "carpal", "periap", "radiograph")):
            return "coleção radiográfica explícita"
        if "images_download_links" in value:
            return "coleção genérica de imagens; origem clínica insuficiente"
        if category == "DOCUMENTATION":
            return "metadados insuficientes; fallback para documentação"
        return "coleção/metadado clínico disponível"

    def _find_manifest(self, record) -> Path:
        matches = []
        for path in self.staging_root.rglob("manifest.json"):
            try:
                manifest = self._read_json(path)
            except CfazRepairError:
                continue
            acquisition = manifest.get("acquisition")
            if not isinstance(acquisition, dict):
                continue
            identifiers = {
                str(acquisition.get(key) or "")
                for key in ("request_id", "provider_request_id", "sequential_id")
            }
            expected = {
                str(record.request_id or ""),
                str(record.provider_request_id or ""),
                str(record.sequential_id or ""),
            }
            if identifiers.intersection(expected) - {""}:
                matches.append(path)
        if len(matches) != 1:
            raise CfazRepairError("Manifesto local do pedido não foi localizado de forma única.")
        return matches[0]

    def _resolve_remote_folder(self, destination: str):
        """Percorre o atalho compartilhado sem tratar o caminho inteiro como drive local."""
        parts = [part.strip() for part in destination.split("/") if part.strip()]
        if not parts:
            raise CfazRepairError("Destino OneDrive inválido no histórico.")
        current = self.graph.find_root_folder(parts[0])
        for expected in parts[1:]:
            normalized = PatientMatcher.normalize_name(expected)
            candidates = [
                item for item in self.graph.list_children(current)
                if isinstance(item.get("folder"), dict)
                and PatientMatcher.normalize_name(str(item.get("name") or ""))
                == normalized
            ]
            if len(candidates) != 1:
                raise CfazRepairError(
                    "Destino remoto do reparo não foi localizado de forma única."
                )
            current = self.graph.folder_from_child_item(current, candidates[0])
        return current

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise CfazRepairError("Manifesto local inválido.") from None
        if not isinstance(value, dict):
            raise CfazRepairError("Manifesto local inválido.")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise CfazRepairError("Não foi possível atualizar o manifesto local.") from None

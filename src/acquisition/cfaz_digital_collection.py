"""Collection-scoped identity for digital model archives."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from hashlib import sha256
import tempfile, zipfile, os, stat
from pathlib import Path
from uuid import uuid4
from .cfaz_digital_identify import normalize_filename


class CfazDigitalCollectionError(RuntimeError):
    pass

@dataclass(frozen=True)
class CollectionSpec:
    request_id: str
    digital_model_id: str
    expected_source_stl_file_ids: frozenset[str]
    identity_scope: str = "COLLECTION"
    individual_source_mapping: None = None
    expected_member_count: int = 2


@dataclass(frozen=True)
class CollectionMember:
    asset_id: str
    sha256: str
    stored_name: str
    size_bytes: int
    digital_model_id: str
    source_stl_file_id: None = None
    source_mapping_status: str = "UNRESOLVED"


@dataclass(frozen=True)
class DigitalModelCollection:
    request_id: str
    digital_model_id: str
    expected_source_stl_file_ids: frozenset[str]
    expected_member_count: int
    identity_scope: str = "COLLECTION"
    individual_source_mapping: None = None
    members: tuple[CollectionMember, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.identity_scope != "COLLECTION" or self.individual_source_mapping is not None:
            raise CfazDigitalCollectionError("Coleção deve permanecer sem mapeamento individual.")
        if len(self.expected_source_stl_file_ids) != self.expected_member_count:
            raise CfazDigitalCollectionError("Quantidade esperada de STL inconsistente.")

    def validate_members(self, members: tuple[CollectionMember, ...]) -> None:
        if len(members) != self.expected_member_count:
            raise CfazDigitalCollectionError("Quantidade de membros da coleção inválida.")
        hashes = [member.sha256 for member in members]
        if len(set(hashes)) != len(hashes):
            raise CfazDigitalCollectionError("Membros duplicados por SHA-256.")
        if any(member.source_stl_file_id is not None or member.source_mapping_status != "UNRESOLVED" for member in members):
            raise CfazDigitalCollectionError("Associação individual não permitida na coleção.")
        if any(member.digital_model_id != self.digital_model_id for member in members):
            raise CfazDigitalCollectionError("Membro pertence a outro modelo digital.")


def collection_from_payload(*, request_id: str, payload: dict) -> DigitalModelCollection:
    models = payload.get("digital_models")
    if not isinstance(models, list) or len(models) != 1:
        raise CfazDigitalCollectionError("COLLECTION exige exatamente um digital_model.")
    model = models[0]
    files = model.get("stl_files") if isinstance(model, dict) else None
    if not isinstance(files, list) or len(files) != 2:
        raise CfazDigitalCollectionError("COLLECTION exige exatamente dois descriptors STL.")
    ids = frozenset(str(item.get("id") or "") for item in files if isinstance(item, dict))
    if len(ids) != 2 or "" in ids:
        raise CfazDigitalCollectionError("stl_file_ids ausentes ou duplicados.")
    if any(item.get("download_url") or item.get("url") for item in files):
        raise CfazDigitalCollectionError("Associação individual disponível; use o fluxo individual.")
    names = [str(item.get("document_file_name") or item.get("filename") or "").strip() for item in files]
    if all(names) and len({normalize_filename(name) for name in names}) == 2:
        raise CfazDigitalCollectionError("Filenames individuais permitem associação; COLLECTION recusada.")
    return DigitalModelCollection(str(request_id), str(model.get("id")), ids, 2)


def validate_zip_single_stl(archive: Path, destination: Path, *, max_members=32, max_total=2_000_000_000) -> tuple[Path, str, int]:
    if not zipfile.is_zipfile(archive):
        raise CfazDigitalCollectionError("Pacote ZIP inválido.")
    destination.mkdir(parents=True, exist_ok=True)
    members = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise CfazDigitalCollectionError("Quantidade de membros excedida.")
        for info in infos:
            name = Path(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if name.is_absolute() or ".." in name.parts or info.is_dir() or stat.S_IFMT(mode) == stat.S_IFLNK:
                raise CfazDigitalCollectionError("ZIP inseguro.")
            if name.suffix.casefold() == ".zip":
                raise CfazDigitalCollectionError("Nested ZIP rejeitado.")
            if name.suffix.casefold() == ".stl":
                members.append(info)
            total += info.file_size
        if len(members) != 1 or total > max_total:
            raise CfazDigitalCollectionError("Pacote deve conter exatamente um STL válido.")
        info = members[0]
        target = destination / "member.stl"
        with zf.open(info) as source, target.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
    digest = sha256(target.read_bytes()).hexdigest()
    return target, digest, target.stat().st_size


@contextmanager
def collection_quarantine():
    with tempfile.TemporaryDirectory(prefix="ireo-cfaz-collection-") as root:
        path = Path(root)
        path.chmod(0o700)
        yield path


class CollectionSession:
    def __init__(self, collection, members, root):
        self.collection, self.members, self.root = collection, members, root


def build_reimport_plan(*, collection: DigitalModelCollection, members: tuple[CollectionMember, ...], current_assets=(), destination=None, snapshot=None) -> dict:
    collection.validate_members(members)
    assets = []
    for member in sorted(members, key=lambda value: value.sha256):
        assets.append({
            "asset_id": member.asset_id,
            "stored_name": member.stored_name,
            "clinical_category": "DIGITAL_MODEL",
            "identity_scope": "COLLECTION",
            "source_stl_file_id": None,
            "source_mapping_status": "UNRESOLVED",
            "sha256": member.sha256,
            "size_bytes": member.size_bytes,
            "relative_path": f"04 - Modelos Digitais/{member.stored_name}",
        })
    return {
        "request_id": collection.request_id,
        "digital_model_id": collection.digital_model_id,
        "identity_scope": "COLLECTION",
        "expected_source_stl_file_ids": sorted(collection.expected_source_stl_file_ids),
        "assets": assets,
        "current_assets": snapshot.clinical_assets_count if snapshot is not None else len(tuple(current_assets)),
        "assets_radiological_planned": snapshot.clinical_assets_count if snapshot is not None else len(tuple(current_assets)),
        "destination": destination,
        "persisted_files": 0,
        "sqlite_changes": 0,
        "onedrive_changes": 0,
        "blockers": list(snapshot.blockers) if snapshot is not None else [],
    }


@contextmanager
def collect_and_validate_collection(*, payload, capture_provider, downloader, limits=None):
    collection = collection_from_payload(request_id=str(payload.get("id") or payload.get("request_id")), payload=payload)
    limits = limits or {}
    candidates = capture_provider(expected_count=collection.expected_member_count)
    if not isinstance(candidates, (set, frozenset, tuple, list)) or len(candidates) != 2:
        raise CfazDigitalCollectionError("Coleção exige exatamente duas candidatas.")
    urls = list(candidates)
    if len(set(urls)) != 2 or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
        raise CfazDigitalCollectionError("Candidatas duplicadas ou inválidas.")
    with collection_quarantine() as root:
        prepared = []
        try:
            for url in urls:
                archive = root / f"{uuid4().hex}.bin"
                downloader(url, archive, limits=limits)
                stl, digest, size = validate_zip_single_stl(
                    archive, root / f"extract-{uuid4().hex}",
                    max_members=limits.get("max_members", 32),
                    max_total=limits.get("max_total", 2_000_000_000),
                )
                prepared.append((stl, digest, size))
            if len({item[1] for item in prepared}) != 2:
                raise CfazDigitalCollectionError("Conteúdos STL duplicados.")
            members = tuple(CollectionMember(
                asset_id=f"cfaz:{collection.request_id}:digital-model:{collection.digital_model_id}:sha256:{digest}",
                sha256=digest,
                stored_name=f"digital-model-{collection.digital_model_id}-{digest[:12]}.stl",
                size_bytes=size,
                digital_model_id=collection.digital_model_id,
            ) for _stl, digest, size in prepared)
            collection.validate_members(members)
            yield CollectionSession(collection, members, root)
        finally:
            prepared.clear()

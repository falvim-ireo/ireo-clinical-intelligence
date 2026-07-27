"""Índice radiológico derivado, versionado e reconstruível a partir dos manifestos."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from typing import Any, Callable, Iterable
from contextlib import nullcontext

from integrations.onedrive_graph import GraphFolder, OneDriveGraphClient, OneDriveGraphError
from services.patient_normalizer import PatientNormalizer


INDEX_VERSION = 1


class ExamIndexError(RuntimeError):
    """Falha segura da camada derivada de indexação."""


@dataclass(frozen=True)
class IndexedExam:
    exam_id: str
    patient_name: str
    exam_date: str | None
    exam_time: str | None
    modality: str | None
    manufacturer: str | None
    model: str | None
    study_count: int
    series_count: int
    status: str
    onedrive_destination: str | None
    index_version: int


@dataclass(frozen=True)
class StructuralComparison:
    left_exam_id: str
    right_exam_id: str
    differences: dict[str, tuple[Any, Any]]

    @property
    def structurally_equal(self) -> bool:
        return not self.differences


@dataclass(frozen=True)
class RebuildResult:
    discovered: int
    indexed: int
    skipped: int
    failed: int
    duration_seconds: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _json_value(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


class ExamIndexService:
    """Persiste somente projeções consultáveis do manifesto clínico."""

    def __init__(self, database_path: str | Path, *, index_version: int = INDEX_VERSION):
        self.database_path = Path(database_path).expanduser().resolve()
        self.index_version = int(index_version)
        if self.index_version < 1:
            raise ValueError("A versão do índice deve ser positiva.")
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise ExamIndexError("Não foi possível inicializar o índice radiológico.") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS patients (
                    patient_key TEXT PRIMARY KEY,
                    normalized_name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    masked_patient_id TEXT,
                    first_exam_date TEXT,
                    last_exam_date TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exams (
                    exam_id TEXT PRIMARY KEY,
                    patient_key TEXT NOT NULL REFERENCES patients(patient_key),
                    exam_date TEXT, exam_time TEXT, exam_date_source TEXT,
                    status TEXT NOT NULL,
                    modality TEXT, manufacturer TEXT, model TEXT,
                    software_versions TEXT, institution TEXT,
                    voxel_json TEXT, fov_json TEXT,
                    study_count INTEGER NOT NULL, series_count INTEGER NOT NULL,
                    source_archive_sha256 TEXT,
                    onedrive_destination TEXT,
                    import_started_at TEXT, import_completed_at TEXT,
                    indexed_at TEXT NOT NULL, index_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS studies (
                    exam_id TEXT NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,
                    study_instance_uid TEXT NOT NULL,
                    study_date TEXT, study_time TEXT, modality TEXT,
                    description TEXT, manufacturer TEXT, model TEXT,
                    software_versions TEXT, institution TEXT,
                    series_count INTEGER NOT NULL,
                    PRIMARY KEY(exam_id, study_instance_uid)
                );
                CREATE TABLE IF NOT EXISTS series (
                    exam_id TEXT NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,
                    study_instance_uid TEXT NOT NULL,
                    series_instance_uid TEXT NOT NULL,
                    modality TEXT, description TEXT,
                    image_count INTEGER NOT NULL,
                    voxel_json TEXT, fov_json TEXT,
                    rows INTEGER, columns INTEGER,
                    PRIMARY KEY(exam_id, series_instance_uid)
                );
                CREATE TABLE IF NOT EXISTS import_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id TEXT, state TEXT NOT NULL, source TEXT NOT NULL,
                    index_version INTEGER NOT NULL, indexed_at TEXT NOT NULL,
                    detail_code TEXT, operation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS exam_assets (
                    exam_id TEXT NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,
                    asset_index INTEGER NOT NULL,
                    source_collection TEXT,
                    stored_name TEXT NOT NULL,
                    relative_folder TEXT NOT NULL,
                    detected_mime TEXT,
                    detected_extension TEXT,
                    asset_type TEXT,
                    clinical_category TEXT,
                    width INTEGER, height INTEGER, size_bytes INTEGER,
                    sha256 TEXT, is_thumbnail INTEGER NOT NULL,
                    is_duplicate INTEGER NOT NULL, duplicate_of TEXT,
                    normalized_at TEXT,
                    PRIMARY KEY(exam_id, asset_index)
                );
                CREATE TABLE IF NOT EXISTS clinical_assets (
                    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id TEXT NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,
                    provider TEXT, provider_request_id TEXT, sequential_id TEXT,
                    patient_id TEXT NOT NULL REFERENCES patients(patient_key),
                    clinical_category TEXT NOT NULL, provider_collection TEXT,
                    provider_section TEXT, provider_display_name TEXT,
                    digital_model_id TEXT, stl_file_id TEXT,
                    stored_name TEXT NOT NULL, relative_path TEXT NOT NULL,
                    mime_type TEXT, extension TEXT, size_bytes INTEGER,
                    sha256 TEXT, width INTEGER, height INTEGER,
                    is_thumbnail INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, normalized_at TEXT,
                    UNIQUE(exam_id, sha256, relative_path)
                );
                CREATE TABLE IF NOT EXISTS consistency_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id TEXT NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,
                    code TEXT NOT NULL, severity TEXT NOT NULL,
                    detail TEXT, UNIQUE(exam_id, code, detail)
                );
                CREATE TABLE IF NOT EXISTS rebuild_state (
                    remote_path TEXT PRIMARY KEY,
                    exam_id TEXT, index_version INTEGER NOT NULL,
                    status TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exams_patient_date ON exams(patient_key, exam_date, exam_time);
                CREATE INDEX IF NOT EXISTS idx_exams_date ON exams(exam_date);
                CREATE INDEX IF NOT EXISTS idx_exams_modality ON exams(modality);
                CREATE INDEX IF NOT EXISTS idx_exams_manufacturer ON exams(manufacturer);
                CREATE INDEX IF NOT EXISTS idx_studies_uid ON studies(study_instance_uid);
                CREATE INDEX IF NOT EXISTS idx_series_uid ON series(series_instance_uid);
                CREATE INDEX IF NOT EXISTS idx_history_state ON import_history(state, indexed_at);
                CREATE INDEX IF NOT EXISTS idx_clinical_assets_patient ON clinical_assets(patient_id);
                CREATE INDEX IF NOT EXISTS idx_clinical_assets_request ON clinical_assets(provider_request_id);
                CREATE INDEX IF NOT EXISTS idx_clinical_assets_category ON clinical_assets(clinical_category);
                CREATE INDEX IF NOT EXISTS idx_clinical_assets_provider ON clinical_assets(provider);
                CREATE INDEX IF NOT EXISTS idx_clinical_assets_sha ON clinical_assets(sha256);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(clinical_assets)")}
            for column in ("digital_model_id", "stl_file_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE clinical_assets ADD COLUMN {column} TEXT")
            history_columns = {row[1] for row in db.execute("PRAGMA table_info(import_history)")}
            if "operation_id" not in history_columns:
                db.execute("ALTER TABLE import_history ADD COLUMN operation_id TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_import_history_operation "
                "ON import_history(operation_id) WHERE operation_id IS NOT NULL"
            )
            db.execute(
                "INSERT INTO index_metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(INDEX_VERSION),),
            )

    @staticmethod
    def _patient_identity(manifest: dict[str, Any], patient_name: str | None) -> tuple[str, str, str]:
        display = _text(patient_name) or ExamIndexService._patient_from_destination(
            _text(manifest.get("onedrive_destination"))
        ) or "Paciente não identificado"
        normalized = PatientNormalizer.compare_ready(display)
        key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return key, normalized, display

    @staticmethod
    def _patient_from_destination(destination: str | None) -> str | None:
        if not destination:
            return None
        parts = [part for part in destination.replace("\\", "/").split("/") if part]
        return parts[-3] if len(parts) >= 3 else None

    def index_manifest(
        self, manifest: dict[str, Any], *, patient_name: str | None = None,
        source: str = "pipeline", connection: sqlite3.Connection | None = None,
        operation_id: str | None = None,
    ) -> IndexedExam:
        publication = manifest.get("publication")
        if not isinstance(publication, dict):
            raise ExamIndexError("Manifesto sem estado de publicação indexável.")
        exam_id = _text(publication.get("exam_id") or publication.get("source_archive_sha256"))
        if not exam_id:
            raise ExamIndexError("Manifesto sem ExamID indexável.")
        intelligence = manifest.get("dicom_intelligence")
        intelligence = intelligence if isinstance(intelligence, dict) else {}
        studies = [item for item in intelligence.get("studies", []) if isinstance(item, dict)]
        patient_key, normalized_name, display_name = self._patient_identity(manifest, patient_name)
        modalities = sorted({_text(item.get("modality")) for item in studies} - {None})
        if not modalities:
            acquisition = manifest.get("acquisition")
            if isinstance(acquisition, dict):
                modalities = sorted({
                    _text(value) for value in acquisition.get("classifications", [])
                } - {None})
        manufacturers = sorted({_text(item.get("manufacturer")) for item in studies} - {None})
        models = sorted({_text(item.get("manufacturer_model_name")) for item in studies} - {None})
        software = sorted({_text(item.get("software_versions")) for item in studies} - {None})
        institutions = sorted({_text(item.get("institution_name")) for item in studies} - {None})
        series_values = [series for study in studies for series in study.get("series", []) if isinstance(series, dict)]
        voxels = [item.get("estimated_voxel_size") for item in series_values if item.get("estimated_voxel_size")]
        fovs = [item.get("estimated_fov") for item in series_values if item.get("estimated_fov")]
        now = _utc_now()
        context = self._connect() if connection is None else nullcontext(connection)
        in_transaction_result = None
        try:
            with context as db:
                if connection is None:
                    db.execute("BEGIN IMMEDIATE")
                if operation_id is not None:
                    prior = db.execute(
                        "SELECT exam_id FROM import_history WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if prior is not None:
                        result = self._row(db.execute(
                            "SELECT e.*,p.display_name FROM exams e JOIN patients p USING(patient_key) WHERE e.exam_id=?",
                            (str(prior["exam_id"]),),
                        ).fetchone()) if connection is not None else self.get_by_exam_id(str(prior["exam_id"]))
                        if result is not None:
                            return result
                db.execute(
                    """INSERT INTO patients(patient_key,normalized_name,display_name,masked_patient_id,
                    first_exam_date,last_exam_date,updated_at) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(patient_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    masked_patient_id=COALESCE(excluded.masked_patient_id,patients.masked_patient_id),
                    updated_at=excluded.updated_at""",
                    (patient_key, normalized_name, display_name, _text(manifest.get("masked_patient_id")),
                     _text(manifest.get("exam_date")), _text(manifest.get("exam_date")), now),
                )
                db.execute(
                    """INSERT INTO exams(exam_id,patient_key,exam_date,exam_time,exam_date_source,status,
                    modality,manufacturer,model,software_versions,institution,voxel_json,fov_json,
                    study_count,series_count,source_archive_sha256,onedrive_destination,
                    import_started_at,import_completed_at,indexed_at,index_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(exam_id) DO UPDATE SET
                    patient_key=excluded.patient_key,exam_date=excluded.exam_date,exam_time=excluded.exam_time,
                    exam_date_source=excluded.exam_date_source,status=excluded.status,
                    modality=excluded.modality,manufacturer=excluded.manufacturer,model=excluded.model,
                    software_versions=excluded.software_versions,institution=excluded.institution,
                    voxel_json=excluded.voxel_json,fov_json=excluded.fov_json,
                    study_count=excluded.study_count,series_count=excluded.series_count,
                    onedrive_destination=excluded.onedrive_destination,
                    import_started_at=excluded.import_started_at,import_completed_at=excluded.import_completed_at,
                    indexed_at=excluded.indexed_at,index_version=excluded.index_version""",
                    (exam_id, patient_key, _text(manifest.get("exam_date")), _text(manifest.get("exam_time")),
                     _text(manifest.get("exam_date_source")), _text(publication.get("state")) or _text(manifest.get("status")) or "UNKNOWN",
                     ",".join(modalities) or None, ",".join(manufacturers) or None,
                     ",".join(models) or None, ",".join(software) or None,
                     ",".join(institutions) or None, _json_value(voxels), _json_value(fovs),
                     len(studies), len(series_values), _text(publication.get("source_archive_sha256")),
                     _text(manifest.get("onedrive_destination")), _text(manifest.get("import_started_at")),
                     _text(manifest.get("import_completed_at")), now, self.index_version),
                )
                db.execute("DELETE FROM studies WHERE exam_id=?", (exam_id,))
                db.execute("DELETE FROM series WHERE exam_id=?", (exam_id,))
                db.execute("DELETE FROM consistency_issues WHERE exam_id=?", (exam_id,))
                db.execute("DELETE FROM exam_assets WHERE exam_id=?", (exam_id,))
                for asset_index, asset in enumerate(
                    (
                        item for item in manifest.get("assets", [])
                        if isinstance(item, dict) and not self._is_ignored_thumbnail(item)
                    ),
                    1,
                ):
                    db.execute(
                        """INSERT INTO exam_assets(
                        exam_id,asset_index,source_collection,stored_name,relative_folder,
                        detected_mime,detected_extension,asset_type,clinical_category,
                        width,height,size_bytes,sha256,is_thumbnail,is_duplicate,
                        duplicate_of,normalized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            exam_id, asset_index, _text(asset.get("source_collection")),
                            _text(asset.get("stored_name")) or f"asset-{asset_index}",
                            _text(asset.get("relative_folder")) or "",
                            _text(asset.get("detected_mime")),
                            _text(asset.get("detected_extension")),
                            _text(asset.get("asset_type")),
                            _text(asset.get("clinical_category")),
                            asset.get("width"), asset.get("height"),
                            asset.get("size_bytes"), _text(asset.get("sha256")),
                            int(bool(asset.get("is_thumbnail"))),
                            int(bool(asset.get("is_duplicate"))),
                            _text(asset.get("duplicate_of")),
                            _text(asset.get("normalized_at")),
                        ),
                    )
                acquisition_meta = manifest.get("acquisition") if isinstance(manifest.get("acquisition"), dict) else {}
                acquisition_package = acquisition_meta.get("clinical_package")
                package = manifest.get("clinical_package") or acquisition_package
                package_assets = package.get("assets") if isinstance(package, dict) else None
                clinical_assets = package_assets if isinstance(package_assets, list) else (
                    manifest.get("assets") if isinstance(manifest.get("assets"), list)
                    else acquisition_meta.get("files", [])
                )
                db.execute("DELETE FROM clinical_assets WHERE exam_id=?", (exam_id,))
                request_meta = manifest.get("request") if isinstance(manifest.get("request"), dict) else {}
                provider = _text(manifest.get("provider") or acquisition_meta.get("provider_id"))
                provider_request_id = _text(request_meta.get("provider_request_id") or acquisition_meta.get("provider_request_id"))
                sequential_id = _text(request_meta.get("sequential_id") or acquisition_meta.get("sequential_id"))
                for asset in clinical_assets:
                    if not isinstance(asset, dict) or self._is_ignored_thumbnail(asset):
                        continue
                    stored = _text(asset.get("stored_name") or asset.get("normalized_filename")) or "asset"
                    folder = _text(asset.get("relative_folder")) or ""
                    relative_path = f"{folder}/{stored}" if folder else stored
                    db.execute(
                        """INSERT INTO clinical_assets(
                        exam_id,provider,provider_request_id,sequential_id,patient_id,
                        clinical_category,provider_collection,provider_section,provider_display_name,
                        stored_name,relative_path,mime_type,extension,size_bytes,sha256,width,height,
                        digital_model_id,stl_file_id,
                        is_thumbnail,created_at,normalized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(exam_id,sha256,relative_path) DO UPDATE SET
                        clinical_category=excluded.clinical_category,provider_collection=excluded.provider_collection,
                        provider_section=excluded.provider_section,provider_display_name=excluded.provider_display_name,
                        stored_name=excluded.stored_name,mime_type=excluded.mime_type,extension=excluded.extension,
                        size_bytes=excluded.size_bytes,width=excluded.width,height=excluded.height,
                        is_thumbnail=excluded.is_thumbnail,normalized_at=excluded.normalized_at""",
                        (exam_id, provider, provider_request_id, sequential_id, patient_key,
                         _text(asset.get("clinical_category")) or "UNKNOWN",
                         _text(asset.get("provider_collection") or asset.get("source_collection")),
                         _text(asset.get("provider_section")), _text(asset.get("provider_display_name")),
                         stored, relative_path,
                         _text(asset.get("mime_type") or asset.get("detected_mime")),
                         _text(asset.get("extension") or asset.get("detected_extension")),
                         asset.get("size") or asset.get("size_bytes"), _text(asset.get("sha256")),
                         asset.get("width"), asset.get("height"),
                         asset.get("digital_model_id") or asset.get("provider_exam_id"),
                         asset.get("stl_file_id") or asset.get("provider_asset_id"),
                         int(bool(asset.get("thumbnail", asset.get("is_thumbnail")))), now,
                         _text(asset.get("normalized_at"))),
                    )
                for study_index, study in enumerate(studies, 1):
                    study_uid = _text(study.get("study_instance_uid")) or f"MISSING-STUDY-{study_index}"
                    child_series = [item for item in study.get("series", []) if isinstance(item, dict)]
                    db.execute(
                        "INSERT INTO studies VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (exam_id, study_uid, _text(study.get("study_date")), _text(study.get("study_time")),
                         _text(study.get("modality")), _text(study.get("study_description")),
                         _text(study.get("manufacturer")), _text(study.get("manufacturer_model_name")),
                         _text(study.get("software_versions")), _text(study.get("institution_name")), len(child_series)),
                    )
                    for series_index, series in enumerate(child_series, 1):
                        series_uid = _text(series.get("series_instance_uid")) or f"MISSING-SERIES-{study_index}-{series_index}"
                        db.execute(
                            "INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (exam_id, study_uid, series_uid, _text(series.get("modality")),
                             _text(series.get("series_description")), int(series.get("image_count") or 0),
                             _json_value(series.get("estimated_voxel_size")),
                             _json_value(series.get("estimated_fov")), series.get("rows"), series.get("columns")),
                        )
                self._record_consistency_issues(db, exam_id, manifest, studies)
                db.execute(
                    "INSERT INTO import_history(exam_id,state,source,index_version,indexed_at,operation_id) VALUES(?,?,?,?,?,?)",
                    (exam_id, "INDEXED", source, self.index_version, now, operation_id),
                )
                db.execute(
                    """UPDATE patients SET
                    first_exam_date=(SELECT MIN(exam_date) FROM exams WHERE patient_key=?),
                    last_exam_date=(SELECT MAX(exam_date) FROM exams WHERE patient_key=?),updated_at=?
                    WHERE patient_key=?""", (patient_key, patient_key, now, patient_key),
                )
                if connection is not None:
                    in_transaction_result = self._row(db.execute(
                        "SELECT e.*,p.display_name FROM exams e JOIN patients p USING(patient_key) WHERE e.exam_id=?",
                        (exam_id,),
                    ).fetchone())
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            if connection is not None:
                raise
            raise ExamIndexError("Não foi possível indexar o manifesto radiológico.") from exc
        result = in_transaction_result or self.get_by_exam_id(exam_id)
        if result is None:
            raise ExamIndexError("O exame não foi encontrado após a indexação.")
        return result

    @staticmethod
    def _is_ignored_thumbnail(asset: dict[str, Any]) -> bool:
        if not bool(asset.get("is_thumbnail", asset.get("thumbnail", False))):
            return False
        collection = str(asset.get("provider_collection") or asset.get("source_collection") or "").casefold()
        category = str(asset.get("clinical_category") or "").upper()
        # Somente preview explicitamente associado a tomografia é indexável.
        return not (
            category == "TOMOGRAPHY"
            and any(term in collection for term in ("preview", "dicom", "tomograph"))
        )

    def _record_consistency_issues(
        self, db: sqlite3.Connection, exam_id: str,
        manifest: dict[str, Any], studies: list[dict[str, Any]],
    ) -> None:
        issues: set[tuple[str, str, str | None]] = set()
        exam_date = _text(manifest.get("exam_date"))
        study_dates = {
            str(value).replace("-", "")
            for item in studies if (value := _text(item.get("study_date")))
        }
        normalized_exam_date = exam_date.replace("-", "") if exam_date else None
        if len(study_dates) > 1 or (
            normalized_exam_date and study_dates and normalized_exam_date not in study_dates
        ):
            issues.add(("INCONSISTENT_DATES", "WARNING", None))
        if any(not item.get("series") for item in studies):
            issues.add(("MISSING_SERIES", "WARNING", None))
        intelligence = manifest.get("dicom_intelligence") or {}
        if int(intelligence.get("patient_count") or 0) > 1:
            issues.add(("INCOMPATIBLE_PATIENT", "ERROR", None))
        for alert in intelligence.get("alerts") or []:
            normalized = PatientNormalizer.compare_ready(str(alert))
            if "PACIENTE" in normalized and "INCOMPAT" in normalized:
                issues.add(("INCOMPATIBLE_PATIENT", "ERROR", None))
            if "UID" in normalized and "DUPLIC" in normalized:
                issues.add(("DUPLICATE_UID", "WARNING", None))
            if "METADADOS" in normalized and "AUSENT" in normalized:
                issues.add(("MISSING_METADATA", "WARNING", None))
        for study in studies:
            uid = _text(study.get("study_instance_uid"))
            if uid and not uid.startswith("MISSING-"):
                other = db.execute(
                    "SELECT exam_id FROM studies WHERE study_instance_uid=? AND exam_id<>? LIMIT 1",
                    (uid, exam_id),
                ).fetchone()
                if other:
                    issues.add(("DUPLICATE_STUDY", "WARNING", uid))
                    other_metadata = db.execute(
                        "SELECT manufacturer,model FROM studies WHERE study_instance_uid=? AND exam_id<>? LIMIT 1",
                        (uid, exam_id),
                    ).fetchone()
                    if other_metadata and (
                        other_metadata["manufacturer"] != _text(study.get("manufacturer"))
                        or other_metadata["model"] != _text(study.get("manufacturer_model_name"))
                    ):
                        issues.add(("CONFLICTING_METADATA", "WARNING", uid))
            manufacturers = {
                _text(series.get("manufacturer")) for series in study.get("series", [])
                if isinstance(series, dict)
            } - {None}
            if len(manufacturers) > 1:
                issues.add(("CONFLICTING_METADATA", "WARNING", uid))
            for series in study.get("series", []):
                if not isinstance(series, dict):
                    continue
                series_uid = _text(series.get("series_instance_uid"))
                if series_uid and db.execute(
                    "SELECT 1 FROM series WHERE series_instance_uid=? AND exam_id<>? LIMIT 1",
                    (series_uid, exam_id),
                ).fetchone():
                    issues.add(("DUPLICATE_SERIES_UID", "WARNING", series_uid))
        for code, severity, detail in issues:
            db.execute(
                "INSERT OR IGNORE INTO consistency_issues(exam_id,code,severity,detail) VALUES(?,?,?,?)",
                (exam_id, code, severity, detail),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> IndexedExam:
        return IndexedExam(
            exam_id=row["exam_id"], patient_name=row["display_name"],
            exam_date=row["exam_date"], exam_time=row["exam_time"],
            modality=row["modality"], manufacturer=row["manufacturer"], model=row["model"],
            study_count=row["study_count"], series_count=row["series_count"],
            status=row["status"], onedrive_destination=row["onedrive_destination"],
            index_version=row["index_version"],
        )

    def _query(self, where: str = "1=1", values: Iterable[Any] = ()) -> list[IndexedExam]:
        sql = """SELECT e.*,p.display_name FROM exams e JOIN patients p USING(patient_key)
                 WHERE """ + where + " ORDER BY e.exam_date,e.exam_time,e.exam_id"
        with self._connect() as db:
            return [self._row(row) for row in db.execute(sql, tuple(values))]

    def get_by_exam_id(self, exam_id: str) -> IndexedExam | None:
        values = self._query("e.exam_id=?", (exam_id,))
        return values[0] if values else None

    def find_by_patient(self, patient_name: str) -> list[IndexedExam]:
        return self._query("p.normalized_name LIKE ?", (f"%{PatientNormalizer.compare_ready(patient_name)}%",))

    def find_by_date(self, exam_date: str) -> list[IndexedExam]:
        return self._query("e.exam_date=?", (exam_date,))

    def find_by_modality(self, modality: str) -> list[IndexedExam]:
        return self._query("UPPER(','||COALESCE(e.modality,'')||',') LIKE ?", (f"%,{modality.strip().upper()},%",))

    def find_by_manufacturer(self, manufacturer: str) -> list[IndexedExam]:
        return self._query("LOWER(COALESCE(e.manufacturer,'')) LIKE ?", (f"%{manufacturer.strip().lower()}%",))

    def find_by_study_uid(self, uid: str) -> list[IndexedExam]:
        return self._query("EXISTS(SELECT 1 FROM studies s WHERE s.exam_id=e.exam_id AND s.study_instance_uid=?)", (uid,))

    def find_by_series_uid(self, uid: str) -> list[IndexedExam]:
        return self._query("EXISTS(SELECT 1 FROM series s WHERE s.exam_id=e.exam_id AND s.series_instance_uid=?)", (uid,))

    def clinical_assets(self, *, patient_id: str | None = None,
                        provider_request_id: str | None = None,
                        clinical_category: str | None = None,
                        provider: str | None = None,
                        sha256: str | None = None) -> list[dict[str, Any]]:
        clauses, values = ["1=1"], []
        join = ""
        if patient_id and len(patient_id) != 64:
            join = " JOIN patients p ON p.patient_key=clinical_assets.patient_id"
            clauses.append("p.normalized_name LIKE ?")
            values.append(f"%{PatientNormalizer.compare_ready(patient_id)}%")
        exact_patient_id = patient_id if not (patient_id and len(patient_id) != 64) else None
        for column, value in (("patient_id", exact_patient_id),
                              ("provider_request_id", provider_request_id),
                              ("clinical_category", clinical_category),
                              ("provider", provider), ("sha256", sha256)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT clinical_assets.* FROM clinical_assets" + join + " WHERE " + " AND ".join(clauses)
                + " ORDER BY exam_id,asset_id", values
            )]

    def search_assets(self, *, patient: str | None = None,
                      category: str | None = None, provider: str | None = None,
                      after: str | None = None, before: str | None = None) -> list[dict[str, Any]]:
        clauses, values = ["1=1"], []
        if patient:
            clauses.append("p.normalized_name LIKE ?")
            values.append(f"%{PatientNormalizer.compare_ready(patient)}%")
        if category:
            clauses.append("LOWER(c.clinical_category) LIKE ?")
            values.append(f"%{category.strip().casefold()}%")
        if provider:
            clauses.append("LOWER(COALESCE(c.provider,''))=?")
            values.append(provider.strip().casefold())
        if after:
            clauses.append("e.exam_date>=?"); values.append(after)
        if before:
            clauses.append("e.exam_date<=?"); values.append(before)
        sql = """SELECT p.display_name patient, e.exam_date date,
                 c.clinical_category category, c.provider provider,
                 COUNT(*) quantity, e.onedrive_destination onedrive
                 FROM clinical_assets c JOIN exams e ON e.exam_id=c.exam_id
                 JOIN patients p ON p.patient_key=c.patient_id
                 WHERE """ + " AND ".join(clauses) + \
              " GROUP BY p.display_name,e.exam_date,c.clinical_category,c.provider,e.onedrive_destination ORDER BY e.exam_date,p.display_name"
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, values)]

    def patient_summary(self, patient_name: str) -> dict[str, Any]:
        exams_for_patient = self.find_by_patient(patient_name)
        patient_keys = {self._patient_key_for_exam(exam.exam_id) for exam in exams_for_patient}
        patient_keys.discard(None)
        if not patient_keys:
            return {"exams": 0, "first_date": None, "last_date": None,
                    "radiographs": 0, "photographs": 0, "tomographies": 0,
                    "dicom": 0, "stl": 0, "digital_models": 0, "reports": 0}
        placeholders = ",".join("?" for _ in patient_keys)
        values = tuple(patient_keys)
        with self._connect() as db:
            exams = db.execute(
                "SELECT COUNT(*) total, MIN(exam_date) first_date, MAX(exam_date) last_date "
                f"FROM exams WHERE patient_key IN ({placeholders})", values
            ).fetchone()
            rows = db.execute(
                "SELECT clinical_category,COUNT(*) total FROM clinical_assets "
                f"WHERE patient_id IN ({placeholders}) GROUP BY clinical_category", values
            ).fetchall()
            dicom = db.execute(
                f"SELECT COUNT(*) FROM clinical_assets WHERE patient_id IN ({placeholders}) AND "
                "(LOWER(COALESCE(mime_type,''))='application/dicom' OR LOWER(COALESCE(extension,''))='.dcm')",
                values,
            ).fetchone()[0]
            stl = db.execute(
                f"SELECT COUNT(*) FROM clinical_assets WHERE patient_id IN ({placeholders}) AND LOWER(COALESCE(extension,''))='.stl'",
                values,
            ).fetchone()[0]
        counts = {str(row["clinical_category"]): int(row["total"]) for row in rows}
        return {
            "exams": int(exams["total"] or 0), "first_date": exams["first_date"],
            "last_date": exams["last_date"], "radiographs": counts.get("RADIOGRAPH", 0),
            "photographs": counts.get("PHOTOGRAPH", 0), "tomographies": counts.get("TOMOGRAPHY", 0),
            "dicom": int(dicom), "stl": int(stl), "digital_models": counts.get("DIGITAL_MODEL", 0),
            "reports": counts.get("REPORT", 0),
        }

    def _patient_key_for_exam(self, exam_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT patient_key FROM exams WHERE exam_id=?", (exam_id,)).fetchone()
        return str(row[0]) if row else None

    def clinical_dashboard(self) -> dict[str, Any]:
        """Estatísticas somente leitura derivadas exclusivamente de clinical_assets."""
        with self._connect() as db:
            totals = db.execute(
                "SELECT COUNT(DISTINCT patient_id), COUNT(DISTINCT exam_id), COUNT(*) "
                "FROM clinical_assets"
            ).fetchone()
            categories = {
                str(row["clinical_category"]): int(row["total"])
                for row in db.execute(
                    "SELECT clinical_category,COUNT(*) total FROM clinical_assets "
                    "GROUP BY clinical_category"
                )
            }
            dicom = db.execute(
                "SELECT COUNT(*) FROM clinical_assets WHERE LOWER(COALESCE(mime_type,''))='application/dicom' "
                "OR LOWER(COALESCE(extension,''))='.dcm'"
            ).fetchone()[0]
            digital = db.execute(
                "SELECT COUNT(*) FROM clinical_assets WHERE clinical_category='DIGITAL_MODEL'"
            ).fetchone()[0]
            last = db.execute("SELECT MAX(created_at) FROM clinical_assets").fetchone()[0]
            duplicate = db.execute(
                "SELECT COUNT(*) FROM clinical_assets WHERE is_thumbnail=0 AND sha256 IN "
                "(SELECT sha256 FROM clinical_assets WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1)"
            ).fetchone()[0]
        return {
            "patients": int(totals[0] or 0), "exams": int(totals[1] or 0),
            "assets": int(totals[2] or 0), "radiographs": categories.get("RADIOGRAPH", 0),
            "photographs": categories.get("PHOTOGRAPH", 0), "tomographies": categories.get("TOMOGRAPHY", 0),
            "dicom": int(dicom), "digital_models": int(digital), "reports": categories.get("REPORT", 0),
            "last_import": last, "orphan_assets": 0, "duplicates": int(duplicate),
        }

    def timeline(self, patient_name: str) -> list[IndexedExam]:
        return self.find_by_patient(patient_name)

    def clinical_timeline(self, patient_name: str) -> list[dict[str, Any]]:
        """Retorna eventos clínicos agregados somente do índice local."""
        exams = self.find_by_patient(patient_name)
        result: list[dict[str, Any]] = []
        for exam in exams:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT clinical_category,COUNT(*) AS total FROM clinical_assets "
                    "WHERE exam_id=? GROUP BY clinical_category", (exam.exam_id,)
                ).fetchall()
                request = db.execute(
                    "SELECT provider,provider_request_id,sequential_id FROM clinical_assets "
                    "WHERE exam_id=? ORDER BY asset_id LIMIT 1", (exam.exam_id,)
                ).fetchone()
            counts = {str(row["clinical_category"]): int(row["total"]) for row in rows}
            result.append({
                "exam_id": exam.exam_id,
                "date": exam.exam_date or "Data não informada",
                "provider": request["provider"] if request else None,
                "provider_request_id": request["provider_request_id"] if request else None,
                "sequential_id": request["sequential_id"] if request else None,
                "counts": counts,
                "status": exam.status,
            })
        return sorted(result, key=lambda item: (
            item["date"],
            ",".join(sorted(item["counts"])),
            str(item["provider"] or ""),
        ))

    def consistency_issues(self, exam_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT exam_id,code,severity,detail FROM consistency_issues "
                + ("WHERE exam_id=? " if exam_id else "") + "ORDER BY exam_id,code",
                (exam_id,) if exam_id else (),
            )
            return [dict(row) for row in rows]

    def compare(self, left_exam_id: str, right_exam_id: str) -> StructuralComparison:
        fields = ("exam_date", "modality", "manufacturer", "model", "voxel_json", "fov_json", "study_count", "series_count")
        with self._connect() as db:
            left = db.execute("SELECT * FROM exams WHERE exam_id=?", (left_exam_id,)).fetchone()
            right = db.execute("SELECT * FROM exams WHERE exam_id=?", (right_exam_id,)).fetchone()
        if left is None or right is None:
            raise ExamIndexError("Um dos exames solicitados não existe no índice.")
        differences = {field: (left[field], right[field]) for field in fields if left[field] != right[field]}
        return StructuralComparison(left_exam_id, right_exam_id, differences)

    def dashboard(self, *, recent_limit: int = 10) -> dict[str, Any]:
        with self._connect() as db:
            patients = db.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
            exams = db.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
            modalities = [dict(row) for row in db.execute(
                "SELECT modality,COUNT(*) count FROM exams WHERE modality IS NOT NULL GROUP BY modality ORDER BY count DESC"
            )]
            manufacturers = [dict(row) for row in db.execute(
                "SELECT manufacturer,COUNT(*) count FROM exams WHERE manufacturer IS NOT NULL GROUP BY manufacturer ORDER BY count DESC"
            )]
            recent = [dict(row) for row in db.execute(
                "SELECT exam_id,state,source,indexed_at FROM import_history ORDER BY id DESC LIMIT ?", (max(1, recent_limit),)
            )]
            failures = db.execute("SELECT COUNT(*) FROM import_history WHERE state='FAILED'").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM rebuild_state WHERE status IN ('PENDING','FAILED')").fetchone()[0]
        return {"patients": patients, "exams": exams, "modalities": modalities,
                "manufacturers": manufacturers, "latest_imports": recent,
                "failures": failures, "pending": pending, "index_version": self.index_version}

    def clear_derived_index(self) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "consistency_issues", "exam_assets", "series", "studies",
                "exams", "patients",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM rebuild_state")


class OneDriveRadiologyIndexRebuilder:
    """Reconstrói o índice lendo apenas manifestos; nunca baixa conteúdo clínico."""

    def __init__(
        self, *, graph: OneDriveGraphClient, onedrive_root: str,
        index: ExamIndexService, output: Callable[[str], None] = print,
        heartbeat_seconds: float = 5.0, max_attempts: int = 5,
        retry_delay_seconds: float = 1.0,
        monotonic_provider: Callable[[], float] = monotonic,
        sleep_provider: Callable[[float], None] = sleep,
    ):
        if heartbeat_seconds <= 0 or max_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("Configuração de timeout/retry do rebuild inválida.")
        self.graph = graph
        self.onedrive_root = onedrive_root
        self.index = index
        self.output = output
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.monotonic_provider = monotonic_provider
        self.sleep_provider = sleep_provider
        self._children_cache: dict[str, tuple[dict[str, Any], ...]] = {}

    def rebuild(self, *, full: bool = False, patient_name: str | None = None) -> RebuildResult:
        total_started = self.monotonic_provider()
        self._children_cache.clear()
        discovered = indexed = skipped = failed = 0
        stage_started = self.monotonic_provider()
        self.output("Localizando pasta raiz...")
        root = self._graph_call(
            "localização da pasta raiz",
            lambda: self.graph.find_root_folder(self.onedrive_root),
        )
        self.output("Localizando pasta raiz... OK")
        self.output("Enumerando pacientes...")
        all_patients = self._folders(root)
        self.output(f"Pacientes encontrados: {len(all_patients)}")
        selected_patients = [
            item for item in all_patients
            if not patient_name or PatientNormalizer.compare_ready(item.name)
            == PatientNormalizer.compare_ready(patient_name)
        ]
        inventory: list[
            tuple[GraphFolder, GraphFolder, GraphFolder, str, dict[str, dict[str, Any]]]
        ] = []
        for position, patient in enumerate(selected_patients, 1):
            self.output(f"Paciente {position}/{len(selected_patients)}")
            radiology = next(
                (folder for folder in self._folders(patient)
                 if PatientNormalizer.compare_ready(folder.name) == "RADIOLOGIA"),
                None,
            )
            if radiology is None:
                continue
            for exam_folder in self._folders(radiology):
                remote_path = "/".join(
                    (self.onedrive_root, patient.name, radiology.name, exam_folder.name)
                )
                file_inventory = {
                    str(item.get("name") or "").casefold(): item
                    for item in self._children(exam_folder)
                    if isinstance(item.get("file"), dict)
                }
                inventory.append(
                    (patient, radiology, exam_folder, remote_path, file_inventory)
                )
        inventory_seconds = self.monotonic_provider() - stage_started
        self.output(f"Inventário concluído: {len(inventory)} exames.")
        if full:
            self.output("Recriando projeções locais do índice...")
            self.index.clear_derived_index()
            self.output("Recriando projeções locais do índice... OK")

        stage_started = self.monotonic_provider()
        for exam_position, (
            patient, _, exam_folder, remote_path, file_inventory
        ) in enumerate(inventory, 1):
            discovered += 1
            self.output(f"Exame {exam_position}/{len(inventory)}")
            if self._already_current(remote_path):
                skipped += 1
                continue
            self._checkpoint(remote_path, None, "PENDING")
            try:
                manifest = self._graph_call(
                    "leitura do manifest.json",
                    lambda folder=exam_folder, files=file_inventory: (
                        self._download_inventory_json(folder, files, "manifest.json")
                    ),
                )
                if not isinstance(manifest, dict):
                    raise ExamIndexError("Pasta de exame sem manifest.json indexável.")
                if not isinstance(manifest.get("dicom_intelligence"), dict):
                    summary = self._graph_call(
                        "leitura do dicom_summary.json",
                        lambda folder=exam_folder, files=file_inventory: (
                            self._download_inventory_json(
                                folder, files, "dicom_summary.json"
                            )
                        ),
                    )
                    if isinstance(summary, dict):
                        manifest = {**manifest, "dicom_intelligence": summary}
                result = self.index.index_manifest(
                    manifest, patient_name=patient.name, source="rebuild"
                )
                self._checkpoint(remote_path, result.exam_id, "COMPLETE")
                indexed += 1
            except Exception as exc:
                self._checkpoint(remote_path, None, "FAILED", type(exc).__name__)
                self._history_failure(type(exc).__name__)
                failed += 1
        indexing_seconds = self.monotonic_provider() - stage_started
        duration = self.monotonic_provider() - total_started
        self.output(
            "Etapas: "
            f"inventário={inventory_seconds:.1f}s; "
            f"indexação={indexing_seconds:.1f}s; total={duration:.1f}s."
        )
        return RebuildResult(
            discovered, indexed, skipped, failed, duration,
            {"inventory": inventory_seconds, "indexing": indexing_seconds},
        )

    def _children(self, parent: GraphFolder) -> tuple[dict[str, Any], ...]:
        cache_key = ":".join(
            filter(None, (parent.drive_id, parent.remote_drive_id, parent.remote_item_id, parent.item_id))
        )
        cached = self._children_cache.get(cache_key)
        if cached is None:
            listed = self._graph_call(
                f"enumeração da pasta {parent.name}",
                lambda: self.graph.list_children(parent),
            )
            cached = tuple(item for item in listed if isinstance(item, dict))
            self._children_cache[cache_key] = cached
        return cached

    def _folders(self, parent: GraphFolder) -> list[GraphFolder]:
        values = []
        converter = getattr(self.graph, "folder_from_child_item", None)
        for item in self._children(parent):
            if not isinstance(item.get("folder"), dict) and not (
                isinstance(item.get("remoteItem"), dict)
                and isinstance(item["remoteItem"].get("folder"), dict)
            ):
                continue
            if callable(converter):
                folder = converter(parent, item)
            else:
                folder = GraphFolder(
                    str(item.get("id") or ""), str(item.get("name") or "")
                )
            if folder.item_id and folder.name:
                values.append(folder)
        return sorted(values, key=lambda item: item.name.casefold())

    def _graph_call(self, operation: str, call: Callable[[], Any]) -> Any:
        for attempt in range(1, self.max_attempts + 1):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(call)
                while True:
                    try:
                        return future.result(timeout=self.heartbeat_seconds)
                    except FutureTimeoutError:
                        self.output(
                            "Microsoft Graph demorando para responder... "
                            f"({operation})"
                        )
                    except OneDriveGraphError as exc:
                        if not self._is_transient_graph_error(exc) or attempt >= self.max_attempts:
                            raise
                        break
            next_attempt = attempt + 1
            self.output(f"Tentativa {next_attempt} de {self.max_attempts}...")
            if self.retry_delay_seconds:
                self.sleep_provider(self.retry_delay_seconds * attempt)
        raise ExamIndexError("Tentativas do Microsoft Graph esgotadas.")

    def _download_inventory_json(
        self, folder: GraphFolder, files: dict[str, dict[str, Any]], filename: str
    ) -> dict[str, Any] | None:
        item = files.get(filename.casefold())
        if item is None:
            return None
        reader = getattr(self.graph, "download_json_child_item", None)
        if callable(reader):
            return reader(folder, item)
        return self.graph.download_json_file(folder, filename)

    @staticmethod
    def _is_transient_graph_error(error: OneDriveGraphError) -> bool:
        return error.http_status is None or error.http_status == 429 or (
            error.http_status is not None and 500 <= error.http_status < 600
        )

    def _already_current(self, path: str) -> bool:
        with self.index._connect() as db:
            row = db.execute("SELECT status,index_version FROM rebuild_state WHERE remote_path=?", (path,)).fetchone()
            return bool(row and row["status"] == "COMPLETE" and row["index_version"] == self.index.index_version)

    def _checkpoint(self, path: str, exam_id: str | None, status: str, error: str | None = None) -> None:
        with self.index._connect() as db:
            db.execute(
                """INSERT INTO rebuild_state(remote_path,exam_id,index_version,status,updated_at,error_code)
                VALUES(?,?,?,?,?,?) ON CONFLICT(remote_path) DO UPDATE SET
                exam_id=excluded.exam_id,index_version=excluded.index_version,status=excluded.status,
                updated_at=excluded.updated_at,error_code=excluded.error_code""",
                (path, exam_id, self.index.index_version, status, _utc_now(), error),
            )

    def _history_failure(self, code: str) -> None:
        with self.index._connect() as db:
            db.execute(
                "INSERT INTO import_history(exam_id,state,source,index_version,indexed_at,detail_code) VALUES(NULL,'FAILED','rebuild',?,?,?)",
                (self.index.index_version, _utc_now(), code[:128]),
            )

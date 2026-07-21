"""Organiza exames radiológicos confirmados no OneDrive clínico."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import json
import logging
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import quote

from integrations.onedrive_graph import (
    GraphFolder,
    OneDriveFolderConflictError,
    OneDriveGraphClient,
    OneDriveGraphError,
)
from radiology.patient_matcher import MatchStatus
from workflows.radiology_workflow import WorkflowResult


class OrganizerStatus(str, Enum):
    """Estados finais possíveis da organização no OneDrive."""

    UPLOADED = "UPLOADED"
    DUPLICATE = "DUPLICATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OneDriveRadiologyResult:
    """Resultado tipado com referências remotas suficientes para auditoria."""

    status: OrganizerStatus
    study_instance_uid: str | None
    remote_path: str | None
    drive_id: str | None
    patient_folder_id: str | None
    study_folder_id: str | None
    uploaded_files: tuple[str, ...]
    errors: tuple[str, ...]


class OneDriveRadiologyOrganizer:
    """Publica apenas estudos com associação exata e evita sobrescritas."""

    def __init__(
        self,
        client: OneDriveGraphClient,
        configured_root: str,
        *,
        processing_date: Callable[[], date] = date.today,
        logger: logging.Logger | None = None,
    ) -> None:
        """Configura o cliente Graph existente e a raiz clínica."""

        self.client = client
        self.configured_root = configured_root
        self.processing_date = processing_date
        self.logger = logger or logging.getLogger(__name__)

    def organize(self, workflow: WorkflowResult) -> OneDriveRadiologyResult:
        """Cria a estrutura remota e envia ZIP e relatório quando permitido."""

        study_uid = (
            workflow.study.study_instance_uid if workflow.study is not None else None
        )
        if (
            workflow.study is None
            or workflow.download is None
            or workflow.patient_match is None
            or workflow.patient_match.status is not MatchStatus.EXACT
            or workflow.patient_match.selected_patient is None
        ):
            self.logger.warning(
                "Upload radiológico bloqueado: revisão manual necessária."
            )
            return self._result(
                OrganizerStatus.REVIEW_REQUIRED,
                study_uid,
                error="Workflow sem estudo válido e associação exata.",
            )

        study = workflow.study
        patient = workflow.patient_match.selected_patient
        archive = workflow.download.path
        if not archive.is_file():
            return self._result(
                OrganizerStatus.FAILED,
                study_uid,
                error="ZIP original não foi encontrado para upload.",
            )

        remote_path: str | None = None
        drive_id: str | None = None
        patient_folder: GraphFolder | None = None
        study_folder: GraphFolder | None = None
        uploaded: list[str] = []
        try:
            root = self.client.find_root_folder(self.configured_root)
            patient_folder = self.client.ensure_folder(root, patient.nome)
            radiology_folder = self.client.ensure_folder(patient_folder, "Radiologia")
            folder_name = f"{self._study_date(study.study_date):%Y-%m-%d} - TCFC"
            remote_path = "/".join(
                (self.configured_root.strip("/\\"), patient.nome, "Radiologia", folder_name)
            )
            drive_id, _ = self._folder_location(radiology_folder)

            study_folder = self.client.find_child_folder(
                radiology_folder, folder_name
            )
            if study_folder is not None:
                existing_uid = self._existing_study_uid(study_folder)
                if existing_uid == study_uid:
                    self.logger.info("Estudo DICOM duplicado detectado no OneDrive.")
                    return self._result(
                        OrganizerStatus.DUPLICATE,
                        study_uid,
                        remote_path=remote_path,
                        drive_id=drive_id,
                        patient_folder=patient_folder,
                        study_folder=study_folder,
                    )
                return self._result(
                    OrganizerStatus.FAILED,
                    study_uid,
                    remote_path=remote_path,
                    drive_id=drive_id,
                    patient_folder=patient_folder,
                    study_folder=study_folder,
                    error="A pasta de destino já existe para outro estudo; sobrescrita recusada.",
                )

            try:
                study_folder = self.client.create_folder(
                    radiology_folder, folder_name
                )
            except OneDriveFolderConflictError:
                study_folder = self.client.find_child_folder(
                    radiology_folder, folder_name
                )
                if study_folder is None:
                    raise
                if self._existing_study_uid(study_folder) == study_uid:
                    return self._result(
                        OrganizerStatus.DUPLICATE,
                        study_uid,
                        remote_path=remote_path,
                        drive_id=drive_id,
                        patient_folder=patient_folder,
                        study_folder=study_folder,
                    )
                raise OneDriveGraphError(
                    "Conflito remoto com estudo diferente; sobrescrita recusada."
                )

            if self.client.list_children(study_folder):
                raise OneDriveGraphError(
                    "A pasta recém-criada não está vazia; upload recusado."
                )

            uploaded_zip = self.client.upload_small_file(study_folder, archive)
            uploaded.append(uploaded_zip.name)
            with tempfile.TemporaryDirectory(
                prefix="ireo_onedrive_report_"
            ) as temporary_dir:
                report_path = Path(temporary_dir) / "report.json"
                report_path.write_text(
                    json.dumps(
                        self._report(workflow),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                uploaded_report = self.client.upload_small_file(
                    study_folder, report_path, remote_filename="report.json"
                )
                uploaded.append(uploaded_report.name)

            self.logger.info("Estudo radiológico enviado ao OneDrive com sucesso.")
            return self._result(
                OrganizerStatus.UPLOADED,
                study_uid,
                remote_path=remote_path,
                drive_id=drive_id,
                patient_folder=patient_folder,
                study_folder=study_folder,
                uploaded_files=tuple(uploaded),
            )
        except Exception as exc:
            error = self._safe_external_error(exc)
            self.logger.error("Falha ao organizar estudo no OneDrive: %s", error)
            return self._result(
                OrganizerStatus.FAILED,
                study_uid,
                remote_path=remote_path,
                drive_id=drive_id,
                patient_folder=patient_folder,
                study_folder=study_folder,
                uploaded_files=tuple(uploaded),
                error=error,
            )

    def _existing_study_uid(self, folder: GraphFolder) -> str | None:
        """Lê o report remoto e retorna o UID usado para idempotência."""

        report_item = next(
            (
                item
                for item in self.client.list_children(folder)
                if str(item.get("name", "")).casefold() == "report.json"
                and isinstance(item.get("file"), dict)
            ),
            None,
        )
        if report_item is None:
            return None
        item_id = report_item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise OneDriveGraphError("report.json remoto sem identificador válido.")
        drive_id, _ = self._folder_location(folder)
        payload = self.client._get(
            f"/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id.strip(), safe='')}/content",
            params={},
        )
        uid = payload.get("study_instance_uid")
        return uid.strip() if isinstance(uid, str) and uid.strip() else None

    def _report(self, workflow: WorkflowResult) -> dict[str, Any]:
        """Produz o relatório inicial sem tokens ou URLs de autenticação."""

        assert workflow.study is not None
        assert workflow.patient_match is not None
        assert workflow.patient_match.selected_patient is not None
        study = workflow.study
        patient = workflow.patient_match.selected_patient
        return {
            "study_instance_uid": study.study_instance_uid,
            "study_date": study.study_date,
            "study_description": study.study_description,
            "patient_id": patient.id,
            "patient_name": patient.nome,
            "match_status": workflow.patient_match.status.value,
            "match_score": workflow.patient_match.score,
            "series_count": study.series_count,
            "image_count": sum(series.image_count for series in study.series),
            "workflow_duration_seconds": workflow.duration_seconds,
        }

    def _study_date(self, value: str | None) -> date:
        """Converte StudyDate DICOM ou usa a data de processamento."""

        if value:
            try:
                return datetime.strptime(value, "%Y%m%d").date()
            except ValueError:
                pass
        return self.processing_date()

    @staticmethod
    def _folder_location(folder: GraphFolder) -> tuple[str, str]:
        """Retorna IDs reais da pasta local ou compartilhada."""

        drive_id = folder.remote_drive_id if folder.is_remote else folder.drive_id
        item_id = folder.remote_item_id if folder.is_remote else folder.item_id
        if not drive_id or not item_id:
            raise OneDriveGraphError("Pasta remota sem driveId ou itemId para auditoria.")
        return drive_id, item_id

    @staticmethod
    def _safe_external_error(exc: Exception) -> str:
        """Preserva mensagens sanitizadas do Graph e oculta detalhes inesperados."""

        if isinstance(exc, OneDriveGraphError):
            return str(exc)
        if isinstance(exc, OSError):
            return "Falha local ao preparar os arquivos para upload."
        return f"Falha externa inesperada ({type(exc).__name__})."

    @staticmethod
    def _result(
        status: OrganizerStatus,
        study_uid: str | None,
        *,
        remote_path: str | None = None,
        drive_id: str | None = None,
        patient_folder: GraphFolder | None = None,
        study_folder: GraphFolder | None = None,
        uploaded_files: tuple[str, ...] = (),
        error: str | None = None,
    ) -> OneDriveRadiologyResult:
        """Constrói resultados uniformes para sucesso, revisão e falha."""

        return OneDriveRadiologyResult(
            status=status,
            study_instance_uid=study_uid,
            remote_path=remote_path,
            drive_id=drive_id,
            patient_folder_id=(
                patient_folder.remote_item_id
                if patient_folder and patient_folder.is_remote
                else patient_folder.item_id if patient_folder else None
            ),
            study_folder_id=(
                study_folder.remote_item_id
                if study_folder and study_folder.is_remote
                else study_folder.item_id if study_folder else None
            ),
            uploaded_files=uploaded_files,
            errors=(error,) if error else (),
        )

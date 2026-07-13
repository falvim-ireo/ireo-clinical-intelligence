"""
Modelo de domínio para exames de imagem.

Representa qualquer exame recebido pela plataforma IREO Clinical Intelligence,
independentemente da origem (TransferNow, PACS, CD, pendrive, etc.).
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class ImagingExam:
    """
    Representa um exame de imagem recebido pela plataforma.
    """

    # ============================
    # Identificação do paciente
    # ============================

    patient_name: str
    patient_id: Optional[int] = None

    # ============================
    # Origem do exame
    # ============================

    source: str = "TransferNow"

    sender_name: Optional[str] = None
    sender_email: Optional[str] = None

    # ============================
    # Arquivos recebidos
    # ============================

    archive_name: Optional[str] = None
    archive_path: Optional[Path] = None

    extracted_folder: Optional[Path] = None

    # ============================
    # Informações DICOM
    # ============================

    modality: Optional[str] = None

    study_date: Optional[str] = None

    study_description: Optional[str] = None

    institution_name: Optional[str] = None

    manufacturer: Optional[str] = None

    number_of_images: int = 0

    # ============================
    # Destino
    # ============================

    onedrive_folder: Optional[Path] = None

    imported: bool = False

    # ============================
    # Auditoria
    # ============================

    received_at: datetime = field(default_factory=datetime.now)

    imported_at: Optional[datetime] = None

    # ============================
    # Utilidades
    # ============================

    @property
    def is_imported(self) -> bool:
        """Retorna True se o exame já foi importado."""

        return self.imported

    @property
    def has_dicom(self) -> bool:
        """Retorna True se existe uma pasta DICOM válida."""

        return self.extracted_folder is not None

    @property
    def patient_display(self) -> str:
        """Nome amigável do paciente."""

        if self.patient_id:
            return f"{self.patient_name} ({self.patient_id})"

        return self.patient_name

    def mark_imported(self):
        """Marca o exame como importado."""

        self.imported = True
        self.imported_at = datetime.now()git add src/radiology

    def __str__(self):

        return (
            f"ImagingExam("
            f"patient='{self.patient_name}', "
            f"modality='{self.modality}', "
            f"source='{self.source}')"
        )
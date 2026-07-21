"""Extração segura de arquivos ZIP para uma área temporária local."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile


class ZipExtractionError(RuntimeError):
    """Indica que um ZIP não pôde ser validado ou extraído com segurança."""


@dataclass(frozen=True)
class ExtractionResult:
    """Resultado de uma extração ZIP concluída."""

    destination: Path
    files: list[Path]
    dicomdir: Path | None


class ZipExtractor:
    """Extrai um ZIP membro a membro, impedindo escrita fora do destino."""

    def __init__(self, temporary_root: str | Path = "tmp") -> None:
        """Configura a pasta sob a qual cada extração terá destino único."""

        self.temporary_root = Path(temporary_root).expanduser().resolve()

    def extract(self, zip_path: str | Path) -> ExtractionResult:
        """Valida e extrai ``zip_path``, retornando arquivos e eventual DICOMDIR."""

        archive = Path(zip_path).expanduser().resolve()
        if not archive.is_file():
            raise ZipExtractionError("O arquivo ZIP não existe.")
        try:
            valid_zip = zipfile.is_zipfile(archive)
        except OSError:
            valid_zip = False
        if not valid_zip:
            raise ZipExtractionError("O arquivo informado não é um ZIP válido.")

        self.temporary_root.mkdir(parents=True, exist_ok=True)
        destination = Path(
            tempfile.mkdtemp(prefix="ireo_zip_", dir=self.temporary_root)
        ).resolve()
        try:
            files = self._extract_members(archive, destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

        dicomdir = next(
            (path for path in files if path.name.casefold() == "dicomdir"),
            None,
        )
        return ExtractionResult(
            destination=destination,
            files=files,
            dicomdir=dicomdir,
        )

    def _extract_members(self, archive: Path, destination: Path) -> list[Path]:
        """Extrai membros previamente validados e retorna somente arquivos."""

        extracted_files: list[Path] = []
        try:
            with zipfile.ZipFile(archive, mode="r") as compressed:
                members = compressed.infolist()
                targets = [
                    self._validated_target(member, destination) for member in members
                ]
                for member, target in zip(members, targets, strict=True):
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with compressed.open(member, mode="r") as source:
                        with target.open(mode="xb") as output:
                            shutil.copyfileobj(source, output)
                    extracted_files.append(target)
        except ZipExtractionError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile):
            raise ZipExtractionError(
                "Não foi possível extrair o arquivo ZIP."
            ) from None
        return sorted(
            extracted_files,
            key=lambda path: path.relative_to(destination).as_posix().casefold(),
        )

    @staticmethod
    def _validated_target(member: zipfile.ZipInfo, destination: Path) -> Path:
        """Resolve um membro e rejeita Zip Slip, caminhos absolutos e symlinks."""

        normalized_name = member.filename.replace("\\", "/")
        member_path = PurePosixPath(normalized_name)
        if (
            not normalized_name
            or member_path.is_absolute()
            or ".." in member_path.parts
        ):
            raise ZipExtractionError("O ZIP contém um caminho inseguro.")

        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ZipExtractionError("Links simbólicos não são aceitos no ZIP.")

        target = destination.joinpath(*member_path.parts).resolve()
        if not target.is_relative_to(destination):
            raise ZipExtractionError("O ZIP contém um caminho inseguro.")
        return target

"""Extração local segura de arquivos ZIP e RAR para a quarentena."""

from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import zipfile


class ArchiveExtractionError(RuntimeError):
    """Falha segura durante validação ou extração de um arquivo."""


class ArchiveExtractor:
    """Extrai sem shell, exclusão ou escrita fora da quarentena."""

    def __init__(
        self,
        quarantine_root: str | Path,
        archive_tool_path: str | Path,
        timeout_seconds: int = 120,
    ) -> None:
        self.quarantine_root = Path(quarantine_root).expanduser().resolve()
        self.archive_tool_path = Path(archive_tool_path).expanduser()
        self.timeout_seconds = timeout_seconds

    def extract(self, archive_path: str | Path) -> Path:
        archive = Path(archive_path).expanduser().resolve()
        if not archive.is_file():
            raise ArchiveExtractionError("O arquivo compactado não existe.")

        extension = archive.suffix.casefold()
        if extension not in {".zip", ".rar"}:
            raise ArchiveExtractionError("Somente arquivos .zip e .rar são aceitos.")

        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        destination = self._next_directory(archive.stem)
        self._ensure_within_quarantine(destination)
        destination.mkdir(parents=False, exist_ok=False)

        if extension == ".zip":
            self._extract_zip(archive, destination)
        else:
            self._extract_rar(archive, destination)
        return destination

    def _extract_zip(self, archive: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive) as compressed:
                members = compressed.infolist()
                for member in members:
                    self._validate_member(member.filename, destination)
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ArchiveExtractionError(
                            "Links simbólicos não são aceitos no arquivo ZIP."
                        )

                for member in members:
                    target = self._member_target(member.filename, destination)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with compressed.open(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
        except ArchiveExtractionError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveExtractionError(
                "Não foi possível extrair o arquivo ZIP."
            ) from None

    def _extract_rar(self, archive: Path, destination: Path) -> None:
        if not self.archive_tool_path.is_file():
            raise ArchiveExtractionError("O executável do WinRAR não foi encontrado.")

        listing = self._run_winrar(["lb", "-p-", str(archive)])
        for member_name in listing.stdout.splitlines():
            if member_name.strip():
                self._validate_member(member_name.strip(), destination)

        target_argument = f"{destination}\\"
        self._run_winrar(
            [
                "x",
                "-o-",
                "-p-",
                str(archive),
                target_argument,
            ]
        )

    def _run_winrar(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [str(self.archive_tool_path), *arguments]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            raise ArchiveExtractionError(
                "O executável do WinRAR não foi encontrado."
            ) from None
        except subprocess.TimeoutExpired:
            raise ArchiveExtractionError(
                "A extração pelo WinRAR excedeu o tempo limite."
            ) from None

        if completed.returncode != 0:
            raise ArchiveExtractionError(
                "O WinRAR não conseguiu processar o arquivo compactado."
            )
        return completed

    def _next_directory(self, archive_stem: str) -> Path:
        safe_stem = "".join(
            character if character.isalnum() or character in " -_" else "-"
            for character in archive_stem
        ).strip(" .") or "archive"
        candidate = self.quarantine_root / safe_stem
        suffix = 2
        while candidate.exists():
            candidate = self.quarantine_root / f"{safe_stem} ({suffix})"
            suffix += 1
        return candidate

    def _validate_member(self, member_name: str, destination: Path) -> None:
        self._member_target(member_name, destination)

    def _member_target(self, member_name: str, destination: Path) -> Path:
        normalized = member_name.replace("\\", "/")
        member = PurePosixPath(normalized)
        if (
            not normalized
            or normalized == "."
            or member.is_absolute()
            or member.drive
            or re.match(r"^[a-zA-Z]:", normalized)
            or ":" in normalized
            or any(part in {"", ".", ".."} for part in member.parts)
        ):
            raise ArchiveExtractionError(
                "O arquivo compactado contém um caminho inseguro."
            )
        target = (destination / Path(*member.parts)).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise ArchiveExtractionError(
                "O arquivo compactado tenta escrever fora da quarentena."
            )
        return target

    def _ensure_within_quarantine(self, path: Path) -> None:
        if not path.resolve().is_relative_to(self.quarantine_root):
            raise ArchiveExtractionError(
                "A extração deve permanecer dentro da quarentena."
            )

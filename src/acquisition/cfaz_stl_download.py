"""Download diagnóstico, temporário e estruturalmente validado de um STL Cfaz."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
import zipfile

import requests

from acquisition.cfaz_provider import CfazProvider


class CfazStlDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class CfazStlDiagnostic:
    received_bytes: int
    content_category: str
    stl_format: str
    element_count: int
    redirects: int
    final_status: int


def validate_stl(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    if size < 15:
        raise CfazStlDownloadError("STL truncado ou estruturalmente inválido.")
    with path.open("rb") as source:
        prefix = source.read(512)
    if size >= 84:
        triangle_count = struct.unpack("<I", prefix[80:84])[0]
        expected_size = 84 + 50 * triangle_count
        if triangle_count > 0 and expected_size == size:
            _validate_binary_stl(path, triangle_count)
            return "binary", triangle_count
    if prefix.lstrip().lower().startswith(b"solid"):
        return "ascii", _validate_ascii_stl(path)
    if size >= 84:
        raise CfazStlDownloadError(
            "Contador ou tamanho do STL binário é incompatível."
        )
    raise CfazStlDownloadError("Conteúdo não corresponde a um STL reconhecido.")


def _validate_binary_stl(path: Path, triangle_count: int) -> None:
    with path.open("rb") as source:
        if len(source.read(84)) != 84:
            raise CfazStlDownloadError("Cabeçalho STL binário truncado.")
        for _ in range(triangle_count):
            record = source.read(50)
            if len(record) != 50:
                raise CfazStlDownloadError("STL binário truncado.")
            values = struct.unpack("<12fH", record)
            if not all(math.isfinite(value) for value in values[:12]):
                raise CfazStlDownloadError(
                    "STL binário contém valores numéricos inválidos."
                )
        if source.read(1):
            raise CfazStlDownloadError(
                "STL binário contém dados além do tamanho declarado."
            )


def _validate_ascii_stl(path: Path) -> int:
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, UnicodeError):
        raise CfazStlDownloadError("STL ASCII possui codificação inválida.") from None
    tokens = [line.strip().split() for line in lines if line.strip()]
    if not tokens or tokens[0][0].casefold() != "solid":
        raise CfazStlDownloadError("STL ASCII sem abertura solid.")
    position = 1
    facets = 0
    while position < len(tokens) and tokens[position][0].casefold() != "endsolid":
        facet = tokens[position]
        if len(facet) != 5 or [item.casefold() for item in facet[:2]] != [
            "facet", "normal",
        ]:
            raise CfazStlDownloadError("Bloco facet do STL ASCII é inválido.")
        _finite_numbers(facet[2:])
        position += 1
        if position >= len(tokens) or [
            item.casefold() for item in tokens[position]
        ] != ["outer", "loop"]:
            raise CfazStlDownloadError("Bloco outer loop ausente.")
        position += 1
        for _ in range(3):
            if (
                position >= len(tokens)
                or len(tokens[position]) != 4
                or tokens[position][0].casefold() != "vertex"
            ):
                raise CfazStlDownloadError("Vértices do STL ASCII são inválidos.")
            _finite_numbers(tokens[position][1:])
            position += 1
        if position >= len(tokens) or [
            item.casefold() for item in tokens[position]
        ] != ["endloop"]:
            raise CfazStlDownloadError("STL ASCII sem endloop.")
        position += 1
        if position >= len(tokens) or [
            item.casefold() for item in tokens[position]
        ] != ["endfacet"]:
            raise CfazStlDownloadError("STL ASCII sem endfacet.")
        position += 1
        facets += 1
    if (
        facets == 0
        or position >= len(tokens)
        or tokens[position][0].casefold() != "endsolid"
        or position != len(tokens) - 1
    ):
        raise CfazStlDownloadError("STL ASCII truncado ou incompleto.")
    return facets


def _finite_numbers(values: list[str]) -> None:
    try:
        parsed = [float(value) for value in values]
    except ValueError:
        raise CfazStlDownloadError("STL contém número inválido.") from None
    if not all(math.isfinite(value) for value in parsed):
        raise CfazStlDownloadError("STL contém número não finito.")


@contextmanager
def download_stl_diagnostic(
    url: str,
    *,
    session: requests.Session | None = None,
    max_file_bytes: int,
    timeout: tuple[float, float] = (10.0, 30.0),
    max_redirects: int = 3,
) -> Iterator[CfazStlDiagnostic]:
    client = session or requests.Session()
    root = Path(tempfile.mkdtemp(prefix="ireo-cfaz-stl-"))
    root.chmod(stat.S_IRWXU)
    try:
        yield _download_and_validate(
            client, url, root, max_file_bytes=max_file_bytes,
            timeout=timeout, max_redirects=max_redirects,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def download_stl_pair_diagnostic(
    url_map: dict[str, str],
    *,
    expected_ids: set[str],
    session: requests.Session | None = None,
    max_file_bytes: int,
    timeout: tuple[float, float] = (10.0, 30.0),
    max_redirects: int = 3,
) -> Iterator[dict[str, CfazStlDiagnostic]]:
    expected = {str(value) for value in expected_ids}
    normalized = (
        {str(key): str(value or "").strip() for key, value in url_map.items()}
        if isinstance(url_map, dict)
        else {}
    )
    if len(expected) != 2 or set(normalized) != expected:
        raise CfazStlDownloadError(
            "Mapa conjunto de STL incompleto ou com identidade inesperada."
        )
    if len(set(normalized.values())) != 2:
        raise CfazStlDownloadError(
            "Mapa conjunto contém URL ausente ou compartilhada."
        )
    for url in normalized.values():
        CfazProvider._validate_download_url(url)
    client = session or requests.Session()
    root = Path(tempfile.mkdtemp(prefix="ireo-cfaz-stl-pair-"))
    root.chmod(stat.S_IRWXU)
    results: dict[str, CfazStlDiagnostic] = {}
    try:
        for identity in sorted(expected, key=lambda value: value.encode("utf-8")):
            candidate_root = root / uuid4().hex
            candidate_root.mkdir(mode=stat.S_IRWXU)
            results[identity] = _download_and_validate(
                client, normalized[identity], candidate_root,
                max_file_bytes=max_file_bytes, timeout=timeout,
                max_redirects=max_redirects,
            )
        yield results
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _download_and_validate(
    session: requests.Session,
    url: str,
    root: Path,
    *,
    max_file_bytes: int,
    timeout: tuple[float, float],
    max_redirects: int,
) -> CfazStlDiagnostic:
    downloaded = root / "payload.bin"
    extracted = root / "model.stl"
    received, content_type, redirects, status = _stream_download(
        session, url, downloaded, max_file_bytes=max_file_bytes,
        timeout=timeout, max_redirects=max_redirects,
    )
    model_path = downloaded
    category = "stl"
    if zipfile.is_zipfile(downloaded):
        category = "zip"
        _extract_single_safe_stl(
            downloaded, extracted, max_file_bytes=max_file_bytes
        )
        model_path = extracted
    elif content_type in {
        "text/html", "application/xhtml+xml", "application/json",
    }:
        raise CfazStlDownloadError(
            "Resposta textual incompatível com um modelo STL."
        )
    stl_format, count = validate_stl(model_path)
    return CfazStlDiagnostic(
        received_bytes=received,
        content_category=category,
        stl_format=stl_format,
        element_count=count,
        redirects=redirects,
        final_status=status,
    )


def _stream_download(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    max_file_bytes: int,
    timeout: tuple[float, float],
    max_redirects: int,
) -> tuple[int, str, int, int]:
    current = str(url or "").strip()
    redirects = 0
    previous_host = None
    while True:
        CfazProvider._validate_download_url(current)
        parts = urlsplit(current)
        headers = {}
        if previous_host and previous_host != parts.hostname:
            headers = {"Authorization": None}
        try:
            response = session.get(
                current, headers=headers, timeout=timeout, stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise CfazStlDownloadError("Tempo limite no download STL.") from None
        except requests.RequestException:
            raise CfazStlDownloadError("Falha de comunicação no download STL.") from None
        if response.status_code in {301, 302, 303, 307, 308}:
            if redirects >= max_redirects:
                raise CfazStlDownloadError(
                    "Quantidade máxima de redirecionamentos excedida."
                )
            location = str(response.headers.get("Location") or "").strip()
            if not location:
                raise CfazStlDownloadError("Redirecionamento sem destino.")
            next_url = urljoin(current, location)
            CfazProvider._validate_download_url(next_url)
            previous_host = parts.hostname
            current = next_url
            redirects += 1
            response.close()
            continue
        if response.status_code != 200:
            response.close()
            raise CfazStlDownloadError(
                f"Download STL rejeitado (HTTP {response.status_code})."
            )
        content_type = str(
            response.headers.get("Content-Type") or ""
        ).split(";", 1)[0].casefold()
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_file_bytes:
            response.close()
            raise CfazStlDownloadError("Arquivo excede o limite configurado.")
        size = 0
        try:
            with destination.open("xb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_file_bytes:
                        raise CfazStlDownloadError(
                            "Arquivo excede o limite durante o streaming."
                        )
                    if size == len(chunk):
                        prefix = chunk[:512].lstrip().lower()
                        if (
                            content_type in {
                                "text/html", "application/xhtml+xml",
                                "application/json",
                            }
                            or prefix.startswith((
                                b"<!doctype html", b"<html", b"{", b"[",
                            ))
                        ):
                            raise CfazStlDownloadError(
                                "Resposta textual incompatível com STL."
                            )
                    output.write(chunk)
        finally:
            response.close()
        if size == 0:
            raise CfazStlDownloadError("Download STL vazio.")
        return size, content_type, redirects, 200


def _extract_single_safe_stl(
    archive_path: Path, destination: Path, *, max_file_bytes: int
) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            stl_entries = []
            total = 0
            normalized_paths: set[str] = set()
            for entry in entries:
                relative = PurePosixPath(entry.filename)
                mode = (entry.external_attr >> 16) & 0o170000
                normalized_path = "/".join(relative.parts).casefold()
                if (
                    relative.is_absolute() or ".." in relative.parts
                    or "\\" in entry.filename or mode == stat.S_IFLNK
                    or entry.flag_bits & 0x1
                ):
                    raise CfazStlDownloadError("Contêiner contém entrada insegura.")
                if normalized_path in normalized_paths:
                    raise CfazStlDownloadError(
                        "Contêiner contém colisão de caminhos."
                    )
                normalized_paths.add(normalized_path)
                total += int(entry.file_size)
                if total > max_file_bytes:
                    raise CfazStlDownloadError(
                        "Conteúdo do contêiner excede o limite configurado."
                    )
                if entry.file_size / max(entry.compress_size, 1) > 1000:
                    raise CfazStlDownloadError(
                        "Contêiner possui expansão desproporcional."
                    )
                if relative.suffix.casefold() == ".stl":
                    stl_entries.append(entry)
            if len(stl_entries) != 1:
                raise CfazStlDownloadError(
                    "Contêiner não possui exatamente um STL."
                )
            written = 0
            with archive.open(stl_entries[0]) as source, destination.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_file_bytes:
                        raise CfazStlDownloadError(
                            "STL extraído excede o limite configurado."
                        )
                    output.write(chunk)
            if written != stl_entries[0].file_size:
                raise CfazStlDownloadError("STL extraído está truncado.")
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        if isinstance(exc, CfazStlDownloadError):
            raise
        raise CfazStlDownloadError("Contêiner STL inválido.") from None

"""Quarantine-only identity validation for Cfaz digital model archives."""
from __future__ import annotations

import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Iterable


class CfazIdentifyError(RuntimeError):
    pass


def normalize_filename(value: str) -> str:
    name = Path(str(value).strip().strip('"\'')).name
    return unicodedata.normalize("NFC", name).casefold()


def validate_descriptor_filenames(descriptors: Iterable[dict]) -> dict[str, str]:
    values = list(descriptors)
    if not values or any(not str(item.get("filename") or "").strip() for item in values):
        raise CfazIdentifyError(
            "Identificação por conteúdo indisponível: os descriptors não possuem nomes únicos."
        )
    result: dict[str, str] = {}
    for item in values:
        key = normalize_filename(str(item["filename"]))
        stl_id = str(item.get("stl_file_id") or "")
        if not stl_id or key in result:
            raise CfazIdentifyError(
                "Identificação por conteúdo indisponível: os descriptors não possuem nomes únicos."
            )
        result[key] = stl_id
    return result


def restricted_quarantine() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="ireo-cfaz-identify-")

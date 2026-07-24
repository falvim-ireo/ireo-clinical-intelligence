"""Adapter do TransferNow para o contrato multiprovedor, sem mudar seu fluxo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acquisition.base import (
    AcquiredPackage, AcquisitionAsset, AcquisitionProvider, AcquisitionRequest,
    AssetClassification,
)
from integrations.transfernow_connector import TransferNowConnector


class TransferNowProvider(AcquisitionProvider):
    provider_id = "transfernow"
    provider_name = "TransferNow"

    def __init__(self, *, downloader) -> None:
        self.downloader = downloader

    def authenticate(self) -> None:
        return None

    def discover(self, notification: Any) -> tuple[AcquisitionRequest, ...]:
        body = "\n".join(
            value for value in (
                getattr(notification, "text_body", None),
                getattr(notification, "html_body", None),
            ) if value
        )
        transfer = TransferNowConnector.interpretar(body, getattr(notification, "subject", ""))
        return (AcquisitionRequest(
            provider_id=self.provider_id,
            request_id=str(getattr(notification, "message_id", "")),
            source_url=transfer.download_url,
            patient_name=transfer.patient_name_candidate,
            request_date=getattr(notification, "received_at", None),
            exam_date=None,
            radiology_clinic=transfer.sender_name,
            professional=transfer.sender_email,
            assets=(AcquisitionAsset(
                asset_id="archive", download_url=transfer.download_url,
                filename=transfer.original_filename,
                classification=AssetClassification.OTHER,
            ),),
        ),)

    def download(self, request, quarantine_root, correlation_id) -> AcquiredPackage:
        asset = request.assets[0]
        result = self.downloader.download(
            asset.download_url, asset.filename, Path(quarantine_root),
            correlation_id, request.request_id,
        )
        return AcquiredPackage(
            request, result.path, result.sha256, 1, result.size_bytes,
            int(bool(getattr(result, "resumed", False))),
        )

    def classify(self, filename: str, metadata=None) -> AssetClassification:
        return AssetClassification.OTHER

    def finalize(self, request: AcquisitionRequest, *, success: bool) -> None:
        return None

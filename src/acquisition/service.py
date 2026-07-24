"""Orquestra um provider e entrega seu pacote ao importador existente."""

from __future__ import annotations

from typing import Any, Callable
from time import monotonic

from acquisition.base import AcquisitionProvider
from acquisition.cfaz_provider import CfazAmbiguousRequestError


class ProviderAcquisitionService:
    def __init__(
        self, *, provider: AcquisitionProvider, importer,
        quarantine_root, correlation_id: str,
        output: Callable[[str], None] = print,
        history=None,
        monotonic_provider: Callable[[], float] = monotonic,
    ) -> None:
        self.provider = provider
        self.importer = importer
        self.quarantine_root = quarantine_root
        self.correlation_id = correlation_id
        self.output = output
        self.history = history
        self.monotonic_provider = monotonic_provider
        self._authenticated = False

    def run(self, notification: Any):
        self._authenticate()
        requests = self.provider.discover(notification)
        return self._run_requests(
            requests,
            message_id=str(getattr(notification, "message_id", "") or ""),
        )

    def run_request_id(self, request_id: str):
        """Executa aquisição explícita sem construir ou consultar catálogo Gmail."""
        self._authenticate()
        discover_request = getattr(self.provider, "discover_request", None)
        if not callable(discover_request):
            raise TypeError("O provider não suporta aquisição por Request ID.")
        try:
            requests = discover_request(request_id)
        except CfazAmbiguousRequestError:
            if self.history is not None:
                self.history.mark_ambiguous(str(request_id))
            raise
        return self._run_requests(requests, message_id="")

    def _authenticate(self) -> None:
        if not self._authenticated:
            self.provider.authenticate()
            self._authenticated = True
            self.output("Login........................ OK")

    def _run_requests(self, requests, *, message_id: str):
        self.output("Detectando novo pedido........ OK")
        results = []
        for request in requests:
            started = self.monotonic_provider()
            if self.history is not None:
                self.history.mark_started(
                    request_id=request.request_id, message_id=message_id,
                    patient_name=request.patient_name,
                    provider_exam_id=request.provider_exam_id,
                    provider_request_id=(
                        getattr(request, "provider_request_id", None)
                        or request.request_id
                    ),
                    sequential_id=getattr(request, "sequential_id", None),
                    clinic_number=getattr(request, "clinic_number", None),
                )
            self.output("Abrindo pedido............... OK")
            self.output("Enumerando exames............ OK")
            try:
                acquired = self.provider.download(
                    request, self.quarantine_root, self.correlation_id
                )
                self.output("Quarentena................... OK")
                metadata = acquired.request.manifest_metadata()
                file_metadata = getattr(acquired, "file_metadata", ())
                metadata["files"] = [dict(item) for item in file_metadata]
                exam_date = request.exam_date.date() if request.exam_date else None
                result = self.importer.run(
                    archive_path=acquired.archive_path,
                    archive_sha256=acquired.sha256,
                    acquisition_metadata=metadata,
                    acquisition_exam_id=self._stable_exam_id(acquired),
                    source_provider=request.provider_id,
                    sender_exam_date=exam_date,
                )
            except Exception as exc:
                duration = self.monotonic_provider() - started
                if self.history is not None:
                    self.history.mark_failed(
                        request_id=request.request_id,
                        patient_name=request.patient_name,
                        duration_seconds=duration,
                        error_code=type(exc).__name__,
                    )
                self.provider.finalize(request, success=False)
                raise
            self.provider.finalize(request, success=True)
            if self.history is not None:
                self.history.mark_complete(
                    request_id=request.request_id,
                    patient_name=request.patient_name,
                    duration_seconds=self.monotonic_provider() - started,
                    onedrive_destination=str(result.onedrive_destination),
                    provider_exam_id=request.provider_exam_id,
                    acquisition_sha=acquired.sha256,
                    provider_request_id=(
                        getattr(request, "provider_request_id", None)
                        or request.request_id
                    ),
                    sequential_id=getattr(request, "sequential_id", None),
                    clinic_number=getattr(request, "clinic_number", None),
                )
            self.output("Pipeline..................... OK")
            self.output("Finalização................. OK")
            results.append(result)
        return tuple(results)

    @staticmethod
    def _stable_exam_id(acquired) -> str:
        import hashlib
        request = acquired.request
        value = "|".join((
            request.provider_id, request.request_id,
            request.provider_exam_id or "", acquired.sha256,
        ))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

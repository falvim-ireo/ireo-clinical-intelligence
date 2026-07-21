import sys
import argparse
import hashlib
from typing import Optional, Sequence

from api.clinicorp_connector import ClinicorpAPI
from intelligence.maintenance_engine import MaintenanceEngine
from services.appointment_service import AppointmentService
from services.patient_service import PatientService
from utils.formatters import (
    formatar_data,
    formatar_horario,
    valor_ou_padrao,
)


def exibir_paciente(paciente) -> None:
    print("\n" + "-" * 60)
    print("PACIENTE")
    print("-" * 60)
    print(f"ID: {paciente.id}")
    print(f"Nome: {valor_ou_padrao(paciente.nome)}")
    print(f"Telefone: {valor_ou_padrao(paciente.telefone)}")
    print(f"E-mail: {valor_ou_padrao(paciente.email)}")
    print(f"Status: {valor_ou_padrao(paciente.status)}")
    print(
        "Data de nascimento: "
        f"{formatar_data(paciente.data_nascimento)}"
    )


def exibir_agendamentos(agendamentos) -> None:
    if not agendamentos:
        print("\nNenhum agendamento foi encontrado.")
        return

    agendamentos_ordenados = sorted(
        agendamentos,
        key=lambda agendamento: agendamento.data,
        reverse=True,
    )

    print(
        f"\n{len(agendamentos_ordenados)} "
        "agendamento(s) encontrado(s):"
    )

    for numero, agendamento in enumerate(
        agendamentos_ordenados,
        start=1,
    ):
        print("\n" + "-" * 60)
        print(f"Agendamento {numero}")
        print("-" * 60)
        print(f"Data: {formatar_data(agendamento.data)}")
        print(
            "Horário: "
            f"{formatar_horario(agendamento.hora_inicio)}"
            " às "
            f"{formatar_horario(agendamento.hora_fim)}"
        )


def exibir_acompanhamento(agendamentos) -> None:
    resultado = MaintenanceEngine.analisar(agendamentos)

    print("\n" + "=" * 60)
    print("ANÁLISE DE ACOMPANHAMENTO")
    print("=" * 60)

    if resultado.ultima_consulta:
        print(
            "Último atendimento identificado: "
            f"{resultado.ultima_consulta.strftime('%d/%m/%Y')}"
        )
        print(
            f"Dias desde o último atendimento: "
            f"{resultado.dias_sem_consulta}"
        )
    else:
        print("Último atendimento: não identificado")

    print(f"Classificação: {resultado.classificacao}")

    if resultado.possui_consulta_futura:
        print(
            "Próxima consulta agendada: "
            f"{resultado.proxima_consulta.strftime('%d/%m/%Y')}"
        )
    else:
        print("Próxima consulta agendada: não identificada")

    print(
        "\nObservação: esta classificação considera qualquer "
        "atendimento registrado. A identificação específica de "
        "manutenção dependerá dos dados de categoria ou procedimento."
    )


def clinicorp_main() -> None:
    print("=" * 60)
    print("IREO Clinical Intelligence")
    print("=" * 60)

    try:
        api = ClinicorpAPI()

        nome = input(
            "\nDigite o nome completo do paciente: "
        ).strip()

        if not nome:
            print("\nNenhum nome foi informado.")
            return

        print("\nConsultando a Clinicorp...")

        pacientes_json = api.buscar_paciente(
            nome,
            somente_ativos=True,
        )

        pacientes = PatientService.converter(pacientes_json)

        if not pacientes:
            print(
                "\nNenhum cadastro ATIVO foi encontrado "
                "com esse nome completo."
            )
            return

        paciente = pacientes[0]

        exibir_paciente(paciente)

        if not paciente.id:
            print("\nO paciente não possui PatientId.")
            return

        print("\nConsultando os agendamentos do paciente...")

        agendamentos_json = api.listar_agendamentos(
            paciente.id
        )

        agendamentos = AppointmentService.converter(
            agendamentos_json
        )

        exibir_agendamentos(agendamentos)
        exibir_acompanhamento(agendamentos)

    except Exception as erro:
        print("\nNão foi possível concluir a consulta.")
        print(f"Detalhes: {erro}")


def build_radiology_workflow(patient_source: str, audit_logger=None):
    """Compõe a fonte de pacientes sem ativar Clinicorp implicitamente."""

    from core.config import Config
    from observability.audit_logger import AuditLogger
    from workflows.imaging_workflow import ImagingWorkflow

    audit = audit_logger or AuditLogger(level=Config.AUDIT_LOG_LEVEL)

    if patient_source == "offline":
        return ImagingWorkflow(audit_logger=audit)

    if patient_source == "clinicorp":
        from repositories.clinicorp_patient_repository import (
            ClinicorpPatientRepository,
        )
        from services.patient_resolver import PatientResolver

        repository = ClinicorpPatientRepository(
            api=ClinicorpAPI(),
            audit_logger=audit,
        )
        return ImagingWorkflow(
            patient_resolver=PatientResolver(
                repository,
                audit_logger=audit,
            ),
            audit_logger=audit,
        )

    raise ValueError("Fonte de pacientes inválida.")


def build_radiology_inbox_processor():
    """Compõe o comando operacional com as integrações existentes."""

    import logging

    from core.config import Config
    from integrations.gmail_connector import GmailConnector
    from integrations.microsoft_graph_auth import MicrosoftGraphAuth
    from integrations.onedrive_graph import OneDriveGraphClient
    from radiology.dicom_reader import DicomReader
    from radiology.inbox_processor import RadiologyInboxProcessor
    from radiology.patient_matcher import PatientMatcher
    from radiology.transfernow_download import TransferNowDownloader
    from radiology.zip_extractor import ZipExtractor
    from repositories.clinicorp_patient_repository import ClinicorpPatientRepository
    from storage.onedrive_radiology_organizer import OneDriveRadiologyOrganizer
    from storage.radiology_storage import RadiologyStorage
    from workflows.radiology_workflow import RadiologyWorkflow

    required = {
        "MS_GRAPH_CLIENT_ID": Config.MS_GRAPH_CLIENT_ID,
        "MS_GRAPH_AUTHORITY": Config.MS_GRAPH_AUTHORITY,
        "MS_GRAPH_SCOPES": Config.MS_GRAPH_SCOPES,
        "MS_GRAPH_ONEDRIVE_ROOT": Config.MS_GRAPH_ONEDRIVE_ROOT,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Configuração Microsoft Graph incompleta.")

    auth = MicrosoftGraphAuth(
        client_id=Config.MS_GRAPH_CLIENT_ID,
        authority=Config.MS_GRAPH_AUTHORITY,
        scopes=Config.MS_GRAPH_SCOPES,
        token_cache_file=Config.MS_GRAPH_TOKEN_CACHE_FILE,
        output=logging.getLogger(__name__).info,
    )
    graph = OneDriveGraphClient(auth.acquire_access_token())
    repository = ClinicorpPatientRepository(ClinicorpAPI())
    workflow = RadiologyWorkflow(
        downloader=TransferNowDownloader(
            connect_timeout=Config.IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS,
            read_timeout=Config.IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS,
            max_download_bytes=Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES,
        ),
        zip_extractor=ZipExtractor(),
        dicom_reader=DicomReader(),
        patient_matcher=PatientMatcher(),
    )
    storage = RadiologyStorage(Config.IREO_RADIOLOGY_STORAGE_PATH)
    return RadiologyInboxProcessor(
        gmail=GmailConnector(),
        workflow=workflow,
        storage=storage,
        organizer=OneDriveRadiologyOrganizer(
            graph, Config.MS_GRAPH_ONEDRIVE_ROOT
        ),
        patient_provider=lambda transfer: repository.find_candidates(
            transfer.patient_name_candidate or ""
        ),
        download_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
    )


def main(argv: Optional[Sequence[str]] = None) -> Optional[int]:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        clinicorp_main()
        return None

    if arguments == ["browser-self-test"]:
        from radiology.transfernow_browser_download import run_browser_self_test

        try:
            run_browser_self_test()
        except Exception as exc:
            print(f"Browser self-test falhou: {type(exc).__name__}")
            return 1
        return 0

    if arguments == ["transfernow-link-diagnosis"]:
        from integrations.gmail_connector import GmailConnector, GmailConnectorError
        from radiology.transfernow_link_diagnosis import run_transfernow_link_diagnosis

        try:
            run_transfernow_link_diagnosis(gmail=GmailConnector())
        except (GmailConnectorError, ValueError) as exc:
            print(f"Diagnóstico não concluído: {exc}")
            return 1
        return 0

    if arguments and arguments[0] == "process-radiology-inbox":
        import logging

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence process-radiology-inbox"
        )
        parser.add_argument("--max-messages", type=int, default=5)
        try:
            options = parser.parse_args(arguments[1:])
            if not 1 <= options.max_messages <= 20:
                raise ValueError("limite inválido")
            logging.basicConfig(
                level=logging.INFO,
                format="%(levelname)s %(name)s: %(message)s",
            )
            summary = build_radiology_inbox_processor().run(
                max_messages=options.max_messages
            )
        except SystemExit as exc:
            return int(exc.code or 0)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Falha global do comando (%s).", type(exc).__name__
            )
            print("Processamento da caixa radiológica não foi iniciado.")
            return 1
        print(
            "Resumo: "
            f"encontradas={summary.found}, "
            f"processadas={summary.processed}, "
            f"duplicadas={summary.duplicates}, "
            f"revisar={summary.review_required}, "
            f"falhas={summary.failed}"
        )
        return 0

    if arguments and arguments[0] == "intake-history":
        from core.config import Config
        from radiology.intake_history import (
            ALLOWED_STATUSES,
            IntakeHistoryError,
            IntakeHistoryRepository,
            sanitized_history_lines,
        )

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence intake-history"
        )
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--status", choices=sorted(ALLOWED_STATUSES))
        parser.add_argument("--correlation-id")
        try:
            options = parser.parse_args(arguments[1:])
            history = IntakeHistoryRepository(Config.IREO_INTAKE_DATABASE_PATH)
            records = history.list_records(
                limit=options.limit,
                status=options.status,
                correlation_id=options.correlation_id,
            )
        except (SystemExit, IntakeHistoryError, ValueError):
            print("Não foi possível consultar o histórico com segurança.")
            return 2
        for line in sanitized_history_lines(records):
            print(line)
        return 0

    if arguments and arguments[0] == "intake-review-list":
        from core.config import Config
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository, sanitized_history_lines
        try:
            records = IntakeHistoryRepository(Config.IREO_INTAKE_DATABASE_PATH).list_records(
                limit=100, status="REVIEW_REQUIRED"
            )
        except IntakeHistoryError:
            print("Não foi possível consultar pendências com segurança.")
            return 2
        for line in sanitized_history_lines(records):
            print(line)
        if not records:
            print("Nenhuma pendência de revisão.")
        return 0

    if arguments and arguments[0] == "intake-review-resume":
        from core.config import Config
        from pathlib import Path
        import hashlib
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
        parser = argparse.ArgumentParser(prog="ireo-clinical-intelligence intake-review-resume")
        parser.add_argument("--correlation-id", required=True)
        try:
            options = parser.parse_args(arguments[1:])
            records = IntakeHistoryRepository(Config.IREO_INTAKE_DATABASE_PATH).list_records(
                limit=100, status="REVIEW_REQUIRED", correlation_id=options.correlation_id
            )
        except (SystemExit, IntakeHistoryError):
            print("Não foi possível localizar a pendência com segurança.")
            return 2
        record = next((item for item in records if item.archive_sha256), None)
        if record is None:
            print("A pendência não possui arquivo local; revise a mensagem no Gmail manualmente.")
            return 1
        archive = next((path for path in Path(Config.IREO_RADIOLOGY_QUARANTINE_PATH).rglob("*")
                        if path.is_file() and _sha256_for_resume(path) == record.archive_sha256), None)
        if archive is None:
            print("Arquivo de quarentena não encontrado; nenhuma ação foi feita.")
            return 1
        return main(["radiology-import-supervised", "--archive-path", str(archive),
                     "--patient-source", "clinicorp"])

    if arguments and arguments[0] == "radiology-auto-status":
        from core.config import Config
        import json
        from pathlib import Path
        try:
            value = json.loads(Path(Config.IREO_AUTO_RUN_SUMMARY_PATH).read_text(encoding="utf-8"))
            print("Última execução:", value.get("finished_at", "não disponível"))
            print("Concluídos:", int(value.get("completed", 0)))
            print("Em revisão:", int(value.get("review_required", 0)))
            print("Falhas:", int(value.get("failed", 0)))
            print("Duplicidades bloqueadas:", int(value.get("duplicates_blocked", 0)))
            return 0
        except (OSError, ValueError, TypeError):
            print("Nenhuma execução automática registrada.")
            return 1

    if arguments and arguments[0] == "radiology-auto-run":
        from core.config import Config
        from integrations.gmail_connector import GmailConnector
        from observability.audit_logger import AuditLogger
        from radiology.auto_run import (
            RadiologyAutoRunner, new_summary, sanitized_failure, write_summary_atomic,
        )
        from radiology.intake_history import IntakeHistoryRepository
        from radiology.supervised_import import SupervisedRadiologyImporter
        from radiology.transfernow_download import TransferNowDownloader
        from radiology.transfernow_browser_download import TransferNowBrowserDownloader
        from repositories.clinicorp_patient_repository import ClinicorpPatientRepository
        summary = new_summary()
        code = 1
        try:
            audit = AuditLogger(level=Config.AUDIT_LOG_LEVEL)
            importer = SupervisedRadiologyImporter(
                patients_root=Config.IREO_ONEDRIVE_PATIENTS_PATH,
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
                archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
                patient_repository=ClinicorpPatientRepository(ClinicorpAPI(), audit_logger=audit),
                audit_logger=audit, auto_select_unambiguous=True,
                auto_select_min_score=.98, patient_source_mode="clinicorp",
                intake_history=IntakeHistoryRepository(Config.IREO_INTAKE_DATABASE_PATH),
            )
            code, summary = RadiologyAutoRunner(
                gmail=GmailConnector(), downloader=TransferNowDownloader(
                    connect_timeout=Config.IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=Config.IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS,
                    max_download_bytes=Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES),
                browser_downloader=TransferNowBrowserDownloader(
                    timeout_seconds=Config.IREO_BROWSER_DOWNLOAD_TIMEOUT_SECONDS,
                    max_download_bytes=Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES,
                    headless=Config.IREO_AUTO_RUN_BROWSER_MODE == "headless",
                    allow_manual_interaction=False, debug=False),
                importer=importer, history=importer.intake_history,
                enabled=Config.IREO_AUTO_RUN_ENABLED, allow_copy=Config.IREO_AUTO_RUN_ALLOW_COPY,
                max_messages=Config.IREO_AUTO_RUN_MAX_MESSAGES,
                browser_mode=Config.IREO_AUTO_RUN_BROWSER_MODE,
                require_exact_match=Config.IREO_AUTO_RUN_REQUIRE_EXACT_MATCH,
                summary_path=Config.IREO_AUTO_RUN_SUMMARY_PATH,
                review_report_path=Config.IREO_AUTO_RUN_REVIEW_REPORT_PATH,
                start_date=Config.IREO_AUTO_RUN_START_DATE,
                debug=Config.IREO_AUTO_RUN_DEBUG,
            ).run()
        except Exception as exc:
            summary.failed += 1
            summary.reason_codes.append("GLOBAL_AUTO_RUN_FAILURE")
            summary.global_failure = sanitized_failure(
                exc, "INITIALIZATION", "GLOBAL_AUTO_RUN_FAILURE",
                debug=Config.IREO_AUTO_RUN_DEBUG,
            )
            code = 1
        finally:
            from datetime import datetime, timezone
            summary.finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            summary.duration_seconds = max(0.0, (
                datetime.fromisoformat(summary.finished_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(summary.started_at.replace("Z", "+00:00"))
            ).total_seconds())
            summary.exit_code = code
            write_summary_atomic(summary, Config.IREO_AUTO_RUN_SUMMARY_PATH)
        if summary.review_required:
            print("Execução concluída com pendências; consulte intake-review-list.")
        elif summary.failed:
            print("Execução automática falhou; consulte radiology-auto-status.")
            if Config.IREO_AUTO_RUN_DEBUG and summary.global_failure:
                failure = summary.global_failure
                print(" | ".join((failure["stage"], failure["exception_type"], failure["reason_code"], failure["sanitized_message"])))
                for key in ("function", "line", "operation", "argument_types"):
                    if key in failure:
                        print(f"{key}: {failure[key]}")
        return code

    if arguments and arguments[0] == "radiology-gmail-dry-run":
        from integrations.gmail_connector import GmailConnectorError
        from radiology.gmail_dry_run import run_gmail_dry_run

        if arguments == ["radiology-gmail-dry-run"]:
            patient_source = "offline"
        elif arguments == [
            "radiology-gmail-dry-run",
            "--patient-source",
            "clinicorp",
        ]:
            patient_source = "clinicorp"
        else:
            print(
                "Uso: ireo-clinical-intelligence radiology-gmail-dry-run "
                "[--patient-source clinicorp]"
            )
            return 2

        try:
            workflow = build_radiology_workflow(patient_source)
            return run_gmail_dry_run(workflow=workflow)
        except (GmailConnectorError, ValueError):
            print("Não foi possível concluir o dry-run com segurança.")
            return 1

    if arguments and arguments[0] == "radiology-import-supervised":
        from core.config import Config
        from integrations.gmail_connector import (
            GmailConnector,
            GmailConnectorError,
        )
        from observability.audit_logger import AuditLogger
        from radiology.archive_extractor import ArchiveExtractionError
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
        from radiology.supervised_import import (
            SupervisedImportCancelled,
            SupervisedImportError,
            SupervisedRadiologyImporter,
        )
        from repositories.patient_repository import EmptyPatientRepository

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence radiology-import-supervised"
        )
        entry = parser.add_mutually_exclusive_group(required=True)
        entry.add_argument("--email-message-id")
        entry.add_argument("--archive-path")
        parser.add_argument(
            "--patient-source",
            choices=("clinicorp", "offline"),
            default="offline",
        )
        parser.add_argument("--force-manual-selection", action="store_true")
        parser.add_argument("--allow-reimport", action="store_true")
        try:
            options = parser.parse_args(arguments[1:])
        except SystemExit:
            return 2

        try:
            audit = AuditLogger(level=Config.AUDIT_LOG_LEVEL)
            if options.patient_source == "clinicorp":
                from repositories.clinicorp_patient_repository import (
                    ClinicorpPatientRepository,
                )

                repository = ClinicorpPatientRepository(
                    ClinicorpAPI(),
                    audit_logger=audit,
                )
            else:
                repository = EmptyPatientRepository()

            importer = SupervisedRadiologyImporter(
                patients_root=Config.IREO_ONEDRIVE_PATIENTS_PATH,
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
                archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
                patient_repository=repository,
                gmail_connector=(
                    GmailConnector() if options.email_message_id else None
                ),
                audit_logger=audit,
                auto_select_unambiguous=Config.IREO_AUTO_SELECT_UNAMBIGUOUS,
                auto_select_min_score=Config.IREO_AUTO_SELECT_MIN_SCORE,
                force_manual_selection=options.force_manual_selection,
                patient_source_mode=options.patient_source,
                intake_history=IntakeHistoryRepository(
                    Config.IREO_INTAKE_DATABASE_PATH
                ),
                allow_reimport=options.allow_reimport,
            )
            result = importer.run(
                archive_path=options.archive_path,
                email_message_id=options.email_message_id,
            )
        except SupervisedImportCancelled as exc:
            print(str(exc))
            return 1
        except (
            ArchiveExtractionError,
            GmailConnectorError,
            IntakeHistoryError,
            SupervisedImportError,
            ValueError,
        ) as exc:
            print(f"Importação não concluída: {exc}")
            return 1

        print(f"Importação concluída: {result.destination}")
        print(f"Manifesto: {result.manifest_path}")
        return 0

    if arguments and arguments[0] == "radiology-import-from-gmail":
        from core.config import Config
        from integrations.gmail_connector import GmailConnector, GmailConnectorError
        from observability.audit_logger import AuditLogger
        from radiology.gmail_import import run_gmail_import
        from radiology.archive_extractor import ArchiveExtractionError
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
        from radiology.supervised_import import (
            SupervisedImportCancelled,
            SupervisedImportError,
            SupervisedRadiologyImporter,
        )
        from radiology.transfernow_download import (
            TransferNowDownloader,
            TransferNowDownloadError,
        )
        from radiology.transfernow_browser_download import TransferNowBrowserDownloader
        from repositories.patient_repository import EmptyPatientRepository

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence radiology-import-from-gmail"
        )
        parser.add_argument(
            "--patient-source", choices=("clinicorp", "offline"), default="offline"
        )
        parser.add_argument("--force-manual-selection", action="store_true")
        parser.add_argument("--allow-reimport", action="store_true")
        try:
            options = parser.parse_args(arguments[1:])
        except SystemExit:
            return 2
        try:
            intake_history = IntakeHistoryRepository(
                Config.IREO_INTAKE_DATABASE_PATH
            )
        except IntakeHistoryError:
            print("Importação não concluída: histórico local indisponível.")
            return 1
        audit = AuditLogger(level=Config.AUDIT_LOG_LEVEL)
        if options.patient_source == "clinicorp":
            from repositories.clinicorp_patient_repository import ClinicorpPatientRepository

            repository = ClinicorpPatientRepository(ClinicorpAPI(), audit_logger=audit)
        else:
            repository = EmptyPatientRepository()
        importer = SupervisedRadiologyImporter(
            patients_root=Config.IREO_ONEDRIVE_PATIENTS_PATH,
            quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
            archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
            archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
            patient_repository=repository,
            audit_logger=audit,
            auto_select_unambiguous=Config.IREO_AUTO_SELECT_UNAMBIGUOUS,
            auto_select_min_score=Config.IREO_AUTO_SELECT_MIN_SCORE,
            force_manual_selection=options.force_manual_selection,
            patient_source_mode=options.patient_source,
            intake_history=intake_history,
            allow_reimport=options.allow_reimport,
        )
        downloader = TransferNowDownloader(
            connect_timeout=Config.IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS,
            read_timeout=Config.IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS,
            max_download_bytes=Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES,
        )
        browser_downloader = TransferNowBrowserDownloader(
            timeout_seconds=Config.IREO_BROWSER_DOWNLOAD_TIMEOUT_SECONDS,
            max_download_bytes=Config.IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES,
            headless=False,
            debug=Config.IREO_BROWSER_DEBUG,
        )
        try:
            outcome = run_gmail_import(
                gmail=GmailConnector(),
                downloader=downloader,
                browser_downloader=browser_downloader,
                importer=importer,
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                correlation_id=audit.correlation_id,
            )
        except (
            ArchiveExtractionError,
            GmailConnectorError,
            IntakeHistoryError,
            SupervisedImportCancelled,
            SupervisedImportError,
            TransferNowDownloadError,
            ValueError,
        ) as exc:
            print(f"Importação não concluída: {exc}")
            return 1
        if outcome.import_result is None:
            print("Download preservado; fluxo supervisionado não iniciado.")
        else:
            print(f"Importação concluída: {outcome.import_result.destination}")
        return 0

    print(
        "Uso: ireo-clinical-intelligence "
        "[radiology-gmail-dry-run | radiology-import-supervised | "
        "radiology-import-from-gmail | browser-self-test | "
        "transfernow-link-diagnosis | intake-history | intake-review-list | intake-review-resume | "
        "radiology-auto-run | radiology-auto-status | process-radiology-inbox]"
    )
    return 2


def _sha256_for_resume(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

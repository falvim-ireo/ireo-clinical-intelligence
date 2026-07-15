import sys
import argparse
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
        try:
            options = parser.parse_args(arguments[1:])
        except SystemExit:
            return 2
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
        "transfernow-link-diagnosis]"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

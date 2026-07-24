import sys
import argparse
import hashlib
import re
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


def _safe_email_diagnostic_field(value: object) -> str:
    """Normaliza header para terminal e remove URLs potencialmente assinadas."""
    single_line = " ".join(str(value or "").split())
    return re.sub(r"https?://\S+", "[URL omitida]", single_line, flags=re.I)[:500]


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
    graph = OneDriveGraphClient(
        auth.acquire_access_token(),
        large_upload_chunk_size=Config.MS_GRAPH_UPLOAD_CHUNK_SIZE_BYTES,
    )
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


def build_onedrive_graph_client():
    """Cria o cliente remoto compartilhado pelos fluxos radiológicos."""
    import logging

    from core.config import Config
    from integrations.microsoft_graph_auth import (
        MicrosoftGraphAuth,
        MicrosoftGraphAuthError,
    )
    from integrations.onedrive_graph import OneDriveGraphClient

    if not Config.MS_GRAPH_ONEDRIVE_ROOT:
        raise ValueError("MS_GRAPH_ONEDRIVE_ROOT não foi configurado.")
    auth = MicrosoftGraphAuth(
        client_id=Config.MS_GRAPH_CLIENT_ID,
        authority=Config.MS_GRAPH_AUTHORITY,
        scopes=Config.MS_GRAPH_SCOPES,
        token_cache_file=Config.MS_GRAPH_TOKEN_CACHE_FILE,
        output=logging.getLogger(__name__).info,
    )
    try:
        token = auth.acquire_access_token()
    except MicrosoftGraphAuthError:
        raise ValueError(
            "Autenticação do Microsoft Graph não foi concluída."
        ) from None
    return OneDriveGraphClient(
        token,
        timeout=(10.0, float(Config.MS_GRAPH_READ_TIMEOUT_SECONDS)),
        large_upload_chunk_size=Config.MS_GRAPH_UPLOAD_CHUNK_SIZE_BYTES,
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

    if arguments and arguments[0] == "find-assets":
        from core.config import Config
        from radiology.exam_index_service import ExamIndexService
        parser = argparse.ArgumentParser(prog="ireo-clinical-intelligence find-assets")
        parser.add_argument("--patient")
        parser.add_argument("--category")
        parser.add_argument("--provider")
        parser.add_argument("--after")
        parser.add_argument("--before")
        try:
            options = parser.parse_args(arguments[1:])
            index = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            rows = index.search_assets(
                patient=options.patient, category=options.category,
                provider=options.provider, after=options.after, before=options.before,
            )
            print("Paciente | Data | Categoria | Provider | Quantidade | OneDrive")
            for row in rows:
                print(
                    f"{row['patient']} | {row['date'] or 'não informada'} | "
                    f"{row['category']} | {row['provider'] or 'não informado'} | "
                    f"{row['quantity']} | {row['onedrive'] or 'não informado'}"
                )
            if not rows:
                print("Nenhum asset encontrado.")
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)

    if arguments and arguments[0] == "patient-summary":
        from core.config import Config
        from radiology.exam_index_service import ExamIndexService
        parser = argparse.ArgumentParser(prog="ireo-clinical-intelligence patient-summary")
        parser.add_argument("--patient", required=True)
        try:
            options = parser.parse_args(arguments[1:])
            summary = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH).patient_summary(options.patient)
            print(f"Paciente\n\nExames:\n    {summary['exams']}\n")
            for label, key in (
                ("Radiografias", "radiographs"), ("Fotografias", "photographs"),
                ("Tomografias", "tomographies"), ("DICOM", "dicom"),
                ("Modelos STL", "stl"), ("Laudos", "reports"),
            ):
                print(f"{label}:\n    {summary[key]}\n")
            first = str(summary["first_date"] or "não informado")[:4]
            last = str(summary["last_date"] or "não informado")[:4]
            print(f"Primeiro exame:\n{first}\n\nÚltimo exame:\n{last}")
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)

    if arguments and arguments[0] == "dashboard":
        from core.config import Config
        from radiology.exam_index_service import ExamIndexService
        summary = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH).clinical_dashboard()
        for label, key in (
            ("Pacientes", "patients"), ("Pedidos", "exams"), ("Assets", "assets"),
            ("Radiografias", "radiographs"), ("Fotografias", "photographs"),
            ("Tomografias", "tomographies"), ("DICOM", "dicom"),
            ("Modelos Digitais", "digital_models"), ("Laudos", "reports"),
            ("Última importação", "last_import"), ("Assets órfãos", "orphan_assets"),
            ("Duplicados", "duplicates"),
        ):
            print(f"{label}: {summary[key] if summary[key] is not None else 'não informado'}")
        return 0

    if arguments and arguments[0] == "clinical-assets":
        import json
        from core.config import Config
        from radiology.exam_index_service import ExamIndexService
        parser = argparse.ArgumentParser(prog="ireo-clinical-intelligence clinical-assets")
        parser.add_argument("--patient")
        parser.add_argument("--provider")
        parser.add_argument("--category")
        try:
            options = parser.parse_args(arguments[1:])
            index = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            for asset in index.clinical_assets(
                patient_id=options.patient, provider=options.provider,
                clinical_category=options.category,
            ):
                print(json.dumps(asset, ensure_ascii=False, default=str))
            return 0
        except (SystemExit, ValueError) as exc:
            return int(exc.code or 0) if isinstance(exc, SystemExit) else 1

    if arguments and arguments[0] == "clinical-assets-rebuild":
        import json
        from pathlib import Path
        from core.config import Config
        from radiology.exam_index_service import ExamIndexError, ExamIndexService
        try:
            index = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            staging = Path(Config.IREO_RADIOLOGY_QUARANTINE_PATH).expanduser() / "supervised-staging"
            indexed = 0
            for manifest_path in staging.rglob("manifest.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                publication = manifest.get("publication")
                if not isinstance(publication, dict) or str(publication.get("state", "")).upper() != "COMPLETE":
                    continue
                # Somente manifestos locais; nenhum provider ou rede é consultado.
                index.index_manifest(manifest, source="clinical-assets-rebuild")
                indexed += len(
                    (manifest.get("clinical_package") or {}).get("assets", [])
                    if isinstance(manifest.get("clinical_package"), dict)
                    else manifest.get("assets", [])
                )
            print(f"Banco: {index.database_path}")
            print(f"Assets indexados: {indexed}")
            with index._connect() as db:
                patients = db.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
                exams = db.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
                assets = db.execute("SELECT COUNT(*) FROM clinical_assets").fetchone()[0]
            print(f"Pacientes: {patients}")
            print(f"Pedidos: {exams}")
            print(f"Assets: {assets}")
            return 0
        except (ExamIndexError, OSError) as exc:
            print(f"Rebuild de clinical_assets não concluído: {exc}")
            return 1

    if arguments and arguments[0] == "patient-timeline":
        from core.config import Config
        from radiology.exam_index_service import ExamIndexService
        parser = argparse.ArgumentParser(prog="ireo-clinical-intelligence patient-timeline")
        parser.add_argument("--patient", required=True)
        try:
            options = parser.parse_args(arguments[1:])
            index = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            events = index.clinical_timeline(options.patient)
            print(f"Paciente\n{'─' * 36}\n")
            labels = {
                "RADIOGRAPH": "Radiografias", "PHOTOGRAPH": "Fotografias",
                "REPORT": "Laudos", "DIGITAL_MODEL": "Modelos Digitais",
                "TOMOGRAPHY": "Tomografias", "DOCUMENTATION": "Documentação",
                "AUXILIARY": "Arquivos Auxiliares", "UNKNOWN": "Não classificados",
            }
            for event in events:
                print(event["date"])
                print("Documentação Radiológica\n")
                for category, label in labels.items():
                    print(f"{label}\n    {event['counts'].get(category, 0)} arquivos\n")
                print(f"Provider\n    {event['provider'] or 'não informado'}\n")
                print(f"Pedido\n    {event['provider_request_id'] or event['sequential_id'] or 'não informado'}\n")
            if not events:
                print("Nenhum exame indexado encontrado.")
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)

    if arguments and arguments[0] == "rebuild-radiology-index":
        from core.config import Config
        from integrations.onedrive_graph import OneDriveGraphError
        from time import monotonic
        from radiology.exam_index_service import (
            ExamIndexError,
            ExamIndexService,
            OneDriveRadiologyIndexRebuilder,
        )

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence rebuild-radiology-index"
        )
        parser.add_argument("--full", action="store_true")
        parser.add_argument("--patient")
        try:
            options = parser.parse_args(arguments[1:])
            progress = lambda message: print(message, flush=True)
            progress("Inicializando rebuild...")
            progress("Preparando índice SQLite local...")
            index = ExamIndexService(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            progress("Preparando índice SQLite local... OK")
            progress("Conectando ao Microsoft Graph...")
            connection_started = monotonic()
            graph = build_onedrive_graph_client()
            connection_seconds = monotonic() - connection_started
            progress("Conectando ao Microsoft Graph... OK")
            result = OneDriveRadiologyIndexRebuilder(
                graph=graph,
                onedrive_root=Config.MS_GRAPH_ONEDRIVE_ROOT,
                index=index,
                output=progress,
                heartbeat_seconds=Config.IREO_REBUILD_HEARTBEAT_SECONDS,
                max_attempts=Config.IREO_REBUILD_MAX_ATTEMPTS,
            ).rebuild(full=options.full, patient_name=options.patient)
            dashboard = index.dashboard()
        except SystemExit as exc:
            return int(exc.code or 0)
        except (ExamIndexError, OneDriveGraphError, ValueError) as exc:
            print(f"Reconstrução não concluída: {exc}")
            return 1
        print(
            "Índice reconstruído: "
            f"encontrados={result.discovered}, indexados={result.indexed}, "
            f"inalterados={result.skipped}, falhas={result.failed}."
        )
        print(
            "Dashboard: "
            f"pacientes={dashboard['patients']}, exames={dashboard['exams']}, "
            f"pendências={dashboard['pending']}, falhas={dashboard['failures']}."
        )
        print(
            "Tempos: "
            f"conexão={connection_seconds:.1f}s; "
            f"inventário={result.stage_seconds.get('inventory', 0.0):.1f}s; "
            f"indexação={result.stage_seconds.get('indexing', 0.0):.1f}s; "
            f"rebuild={result.duration_seconds:.1f}s."
        )
        return 1 if result.failed else 0

    if arguments and arguments[0] == "radiology-auto-run":
        from core.config import Config
        from integrations.gmail_connector import GmailConnector
        from observability.audit_logger import AuditLogger
        from radiology.auto_run import (
            RadiologyAutoRunner, new_summary, sanitized_failure, write_summary_atomic,
        )
        from radiology.intake_history import IntakeHistoryRepository
        from radiology.exam_index_service import ExamIndexService
        from radiology.supervised_import import SupervisedRadiologyImporter
        from radiology.transfernow_download import TransferNowDownloader
        from radiology.transfernow_browser_download import TransferNowBrowserDownloader
        from repositories.clinicorp_patient_repository import ClinicorpPatientRepository
        summary = new_summary()
        code = 1
        try:
            audit = AuditLogger(level=Config.AUDIT_LOG_LEVEL)
            importer = SupervisedRadiologyImporter(
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
                onedrive_client=build_onedrive_graph_client(),
                onedrive_root=Config.MS_GRAPH_ONEDRIVE_ROOT,
                archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
                patient_repository=ClinicorpPatientRepository(ClinicorpAPI(), audit_logger=audit),
                audit_logger=audit, auto_select_unambiguous=True,
                auto_select_min_score=.98, patient_source_mode="clinicorp",
                intake_history=IntakeHistoryRepository(Config.IREO_INTAKE_DATABASE_PATH),
                exam_index_service=ExamIndexService(
                    Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
                ),
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
        from radiology.exam_index_service import ExamIndexError, ExamIndexService
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
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
                onedrive_client=build_onedrive_graph_client(),
                onedrive_root=Config.MS_GRAPH_ONEDRIVE_ROOT,
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
                exam_index_service=ExamIndexService(
                    Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
                ),
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
            ExamIndexError,
            SupervisedImportError,
            ValueError,
        ) as exc:
            print(f"Importação não concluída: {exc}")
            return 1

        print(f"Importação concluída: {result.onedrive_destination}")
        print(f"Manifesto: {result.manifest_path}")
        return 0

    if arguments and arguments[0] == "radiology-import-from-gmail":
        from core.config import Config
        from integrations.gmail_connector import GmailConnector, GmailConnectorError
        from observability.audit_logger import AuditLogger
        from radiology.gmail_import import run_gmail_import
        from radiology.archive_extractor import ArchiveExtractionError
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
        from radiology.exam_index_service import ExamIndexError, ExamIndexService
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
            quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
            archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
            onedrive_client=build_onedrive_graph_client(),
            onedrive_root=Config.MS_GRAPH_ONEDRIVE_ROOT,
            archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
            patient_repository=repository,
            audit_logger=audit,
            auto_select_unambiguous=Config.IREO_AUTO_SELECT_UNAMBIGUOUS,
            auto_select_min_score=Config.IREO_AUTO_SELECT_MIN_SCORE,
            force_manual_selection=options.force_manual_selection,
            patient_source_mode=options.patient_source,
            intake_history=intake_history,
            allow_reimport=options.allow_reimport,
            exam_index_service=ExamIndexService(
                Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
            ),
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
            allow_manual_interaction=False,
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
            ExamIndexError,
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
            print(
                "Importação concluída: "
                f"{outcome.import_result.onedrive_destination}"
            )
        return 0

    if arguments and arguments[0] == "cfaz-list-notifications":
        from acquisition.cfaz_operations import (
            CfazHistoryError, CfazHistoryRepository, CfazNotificationCatalog,
        )
        from core.config import Config
        from integrations.gmail_connector import GmailConnector, GmailConnectorError

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence cfaz-list-notifications"
        )
        parser.add_argument("--debug", action="store_true")
        try:
            options = parser.parse_args(arguments[1:])
            gmail = GmailConnector()
            account = gmail.authenticated_account()
            history = CfazHistoryRepository(
                Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
            )
            catalog = CfazNotificationCatalog(
                gmail=gmail, history=history,
                query=Config.CFAZ_GMAIL_QUERY,
                limit=Config.CFAZ_GMAIL_MAX_MESSAGES,
            )
            notifications = catalog.list()
        except SystemExit as exc:
            return int(exc.code or 0)
        except (CfazHistoryError, GmailConnectorError) as exc:
            print(f"Não foi possível listar notificações Cfaz: {exc}")
            return 1
        print(f"Conta Gmail autenticada: {account}")
        print(f"Consulta Gmail: {Config.CFAZ_GMAIL_QUERY}")
        print(f"Mensagens retornadas pelo Gmail: {catalog.counters.gmail_messages}")
        print(f"Mensagens com assunto CfazPost: {catalog.counters.cfazpost_subjects}")
        print(f"Mensagens com link reconhecido: {catalog.counters.recognized_links}")
        print(f"Notificações operacionais: {catalog.counters.operational_notifications}")
        if options.debug:
            print("\nDiagnóstico das mensagens:")
            for position, item in enumerate(catalog.diagnostics, 1):
                print(f"\n[{position}]")
                print(f"From: {_safe_email_diagnostic_field(item.message.sender)}")
                print(f"Subject: {_safe_email_diagnostic_field(item.message.subject)}")
                print(
                    "Data: "
                    f"{item.received_at.astimezone().strftime('%d/%m/%Y %H:%M')}"
                )
                print(f"Link encontrado: {'sim' if item.link_found else 'não'}")
                print(
                    "Request ID encontrado: "
                    f"{'sim' if item.request_id else 'não'}"
                )
                print(f"Aceita: {'sim' if item.accepted else 'não'}")
                print(f"Motivo da rejeição: {item.rejection_reason or 'nenhum'}")
        print("Notificações encontradas:")
        for position, item in enumerate(notifications, 1):
            print(f"\n[{position}]")
            print(f"Paciente: {item.patient_name or 'não informado'}")
            print(f"Pedido: {item.request_id or 'não identificado'}")
            print(f"Data: {item.received_at.astimezone().strftime('%d/%m/%Y %H:%M')}")
            if not item.request_id:
                print("Status: notificação reconhecida, pedido não identificado")
            else:
                print(f"Status: {'JÁ IMPORTADO' if item.imported else 'NÃO IMPORTADO'}")
        if not notifications:
            print("Nenhuma notificação Cfaz encontrada.")
        return 0

    if arguments and arguments[0] == "cfaz-history":
        from acquisition.cfaz_operations import CfazHistoryError, CfazHistoryRepository
        from core.config import Config

        try:
            records = CfazHistoryRepository(
                Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
            ).list_records()
        except CfazHistoryError as exc:
            print(f"Não foi possível consultar o histórico Cfaz: {exc}")
            return 1
        if not records:
            print("Nenhuma importação Cfaz registrada.")
            return 0
        for record in records:
            print(
                " | ".join((
                    record.completed_at or record.started_at or "data indisponível",
                    record.patient_name or "paciente não informado",
                    f"pedido={record.sequential_id or record.request_id}",
                    f"provider={record.provider}",
                    f"status={record.status}",
                    f"duração={record.duration_seconds:.1f}s"
                    if record.duration_seconds is not None else "duração=indisponível",
                    f"OneDrive={record.onedrive_destination or 'não disponível'}",
                ))
            )
        return 0

    if arguments and arguments[0] == "cfaz-repair-files":
        from pathlib import Path
        from acquisition.cfaz_operations import CfazHistoryError, CfazHistoryRepository
        from acquisition.cfaz_repair import CfazCompletedImportRepair, CfazRepairError
        from core.config import Config
        from integrations.onedrive_graph import OneDriveGraphError

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence cfaz-repair-files"
        )
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--request-id", action="append")
        group.add_argument("--all", action="store_true")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", default=False,
            help="apenas diagnostica; é o modo padrão operacional",
        )
        mode.add_argument(
            "--apply", action="store_true",
            help="aplica renomes, movimentos e atualizações no OneDrive",
        )
        try:
            options = parser.parse_args(arguments[1:])
            history = CfazHistoryRepository(Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH)
            repair = CfazCompletedImportRepair(
                history=history,
                graph=build_onedrive_graph_client() if options.apply else None,
                staging_root=(
                    Path(Config.IREO_RADIOLOGY_QUARANTINE_PATH)
                    / "supervised-staging"
                ),
            )
            identifiers = (
                [
                    record.provider_request_id or record.request_id
                    for record in history.list_completed()
                ]
                if options.all else options.request_id
            )
            for identifier in identifiers:
                apply = bool(options.apply)
                result = repair.repair(identifier, apply=apply)
                print(
                    f"Pedido {identifier}: modo={'APPLY' if apply else 'DRY-RUN'}, "
                    f"arquivos={result.jpeg_files}, renomeados={result.renamed_files}, "
                    f"duplicados={result.duplicate_files}, "
                    f"repair_version={result.repair_version}."
                )
        except SystemExit as exc:
            return int(exc.code or 0)
        except (CfazHistoryError, CfazRepairError, OneDriveGraphError, ValueError) as exc:
            print(f"Reparo Cfaz não concluído: {exc}")
            return 1
        return 0

    if arguments and arguments[0] == "radiology-import-from-cfaz":
        from acquisition.base import AcquisitionError
        from acquisition.cfaz_provider import CfazProvider
        from acquisition.cfaz_operations import (
            CfazHistoryError, CfazHistoryRepository, CfazNotificationCatalog,
        )
        from acquisition.service import ProviderAcquisitionService
        from core.config import Config
        from integrations.gmail_connector import GmailConnector, GmailConnectorError
        from observability.audit_logger import AuditLogger
        from radiology.archive_extractor import ArchiveExtractionError
        from radiology.exam_index_service import ExamIndexService
        from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
        from radiology.supervised_import import (
            SupervisedImportError, SupervisedRadiologyImporter,
        )
        from repositories.clinicorp_patient_repository import ClinicorpPatientRepository

        parser = argparse.ArgumentParser(
            prog="ireo-clinical-intelligence radiology-import-from-cfaz"
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--request-id")
        mode.add_argument("--select", action="store_true")
        parser.add_argument(
            "--debug-auth", "--auth-debug", dest="debug_auth",
            action="store_true",
            help="exibe diagnóstico sanitizado da autenticação Cfaz",
        )
        parser.add_argument(
            "--debug-payload", action="store_true",
            help="exibe somente a estrutura sanitizada do payload Cfaz",
        )
        try:
            options = parser.parse_args(arguments[1:])
            audit = AuditLogger(level=Config.AUDIT_LOG_LEVEL)
            history = CfazHistoryRepository(
                Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
            )
            explicit_request_id = None
            if options.request_id:
                explicit_request_id = str(options.request_id).strip()
                if not explicit_request_id.isdigit():
                    raise AcquisitionError("O Request ID do Cfaz é inválido.")
                if history.is_imported(explicit_request_id):
                    print("O pedido informado já foi importado.")
                    return 0
            else:
                gmail = GmailConnector()
                catalog = CfazNotificationCatalog(
                    gmail=gmail, history=history, query=Config.CFAZ_GMAIL_QUERY,
                    limit=Config.CFAZ_GMAIL_MAX_MESSAGES,
                )
                notifications = catalog.list()
                pending = catalog.pending(notifications)
                imported_count = sum(
                    1 for item in notifications if item.request_id and item.imported
                )
                unidentified_count = sum(
                    1 for item in notifications if not item.request_id
                )
                print(f"{len(notifications)} notificações encontradas")
                print(f"{imported_count} já importadas")
                if unidentified_count:
                    print(f"{unidentified_count} com pedido não identificado")
                print(f"{len(pending)} novas")
                if options.select:
                    print("\nNotificações pendentes")
                    for position, item in enumerate(pending, 1):
                        print(f"{position} {item.patient_name or 'Paciente não informado'}")
                    if not pending:
                        print("Nenhuma notificação pendente.")
                        return 0
                    try:
                        choice = int(input("Escolha: ").strip())
                    except ValueError:
                        raise AcquisitionError("Seleção inválida.") from None
                    if not 1 <= choice <= len(pending):
                        raise AcquisitionError("Seleção inválida.")
                    selected = [pending[choice - 1]]
                else:
                    selected = pending
                if not selected:
                    print("Nenhuma notificação nova para importar.")
                    return 0
            importer = SupervisedRadiologyImporter(
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                archive_tool_path=Config.IREO_ARCHIVE_TOOL_PATH,
                onedrive_client=build_onedrive_graph_client(),
                onedrive_root=Config.MS_GRAPH_ONEDRIVE_ROOT,
                archive_timeout_seconds=Config.IREO_ARCHIVE_TIMEOUT_SECONDS,
                patient_repository=ClinicorpPatientRepository(
                    ClinicorpAPI(), audit_logger=audit
                ),
                audit_logger=audit, auto_select_unambiguous=True,
                auto_select_min_score=Config.IREO_AUTO_SELECT_MIN_SCORE,
                patient_source_mode="clinicorp",
                intake_history=IntakeHistoryRepository(
                    Config.IREO_INTAKE_DATABASE_PATH
                ),
                exam_index_service=ExamIndexService(
                    Config.IREO_RADIOLOGY_INDEX_DATABASE_PATH
                ),
            )
            provider = CfazProvider(
                api_token=Config.CFAZ_API_TOKEN,
                email=Config.CFAZ_EMAIL,
                password=Config.CFAZ_PASSWORD,
                timeout=(10.0, float(Config.CFAZ_API_TIMEOUT_SECONDS)),
                max_file_bytes=Config.CFAZ_MAX_FILE_BYTES,
                auth_diagnostics=options.debug_auth,
                payload_diagnostics=options.debug_payload,
            )
            service = ProviderAcquisitionService(
                provider=provider, importer=importer,
                quarantine_root=Config.IREO_RADIOLOGY_QUARANTINE_PATH,
                correlation_id=audit.correlation_id,
                history=history,
            )
            print("Importando...")
            if explicit_request_id:
                imported = list(service.run_request_id(explicit_request_id))
            else:
                imported = [
                    result for item in selected for result in service.run(item.message)
                ]
        except SystemExit as exc:
            return int(exc.code or 0)
        except (
            AcquisitionError, ArchiveExtractionError, CfazHistoryError,
            GmailConnectorError, IntakeHistoryError, SupervisedImportError, ValueError,
        ) as exc:
            print(f"Aquisição Cfaz não concluída: {exc}")
            return 1
        print(f"Pedidos Cfaz concluídos: {len(imported)}")
        return 0

    print(
        "Uso: ireo-clinical-intelligence "
        "[radiology-gmail-dry-run | radiology-import-supervised | "
        "radiology-import-from-gmail | radiology-import-from-cfaz | "
        "cfaz-list-notifications | cfaz-history | browser-self-test | "
        "cfaz-repair-files | "
        "transfernow-link-diagnosis | intake-history | intake-review-list | intake-review-resume | "
        "radiology-auto-run | radiology-auto-status | rebuild-radiology-index | "
        "process-radiology-inbox]"
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

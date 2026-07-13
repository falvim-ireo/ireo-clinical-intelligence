import sys
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


def main(argv: Optional[Sequence[str]] = None) -> Optional[int]:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        clinicorp_main()
        return None

    if arguments == ["radiology-gmail-dry-run"]:
        from integrations.gmail_connector import GmailConnectorError
        from radiology.gmail_dry_run import run_gmail_dry_run

        try:
            return run_gmail_dry_run()
        except GmailConnectorError:
            print("Não foi possível acessar o Gmail com segurança.")
            return 1

    print(
        "Uso: ireo-clinical-intelligence "
        "[radiology-gmail-dry-run]"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

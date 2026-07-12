from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from models.appointment import Appointment


@dataclass
class MaintenanceResult:
    ultima_consulta: Optional[date]
    dias_sem_consulta: Optional[int]
    classificacao: str
    possui_consulta_futura: bool
    proxima_consulta: Optional[date]


class MaintenanceEngine:
    """
    Analisa o intervalo desde o último atendimento registrado.

    Nesta fase, o resultado representa o tempo desde a última consulta,
    não necessariamente desde a última manutenção clínica.
    """

    @staticmethod
    def _converter_data(valor: str) -> Optional[date]:
        if not valor:
            return None

        texto = str(valor).strip()

        formatos = (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        )

        for formato in formatos:
            try:
                return datetime.strptime(texto, formato).date()
            except ValueError:
                continue

        return None

    @classmethod
    def analisar(
        cls,
        agendamentos: list[Appointment],
        data_referencia: Optional[date] = None,
    ) -> MaintenanceResult:
        hoje = data_referencia or date.today()

        datas_validas = []

        for agendamento in agendamentos:
            data_agendamento = cls._converter_data(
                agendamento.data
            )

            if data_agendamento:
                datas_validas.append(data_agendamento)

        consultas_passadas = [
            data_consulta
            for data_consulta in datas_validas
            if data_consulta <= hoje
        ]

        consultas_futuras = [
            data_consulta
            for data_consulta in datas_validas
            if data_consulta > hoje
        ]

        ultima_consulta = (
            max(consultas_passadas)
            if consultas_passadas
            else None
        )

        proxima_consulta = (
            min(consultas_futuras)
            if consultas_futuras
            else None
        )

        if ultima_consulta is None:
            return MaintenanceResult(
                ultima_consulta=None,
                dias_sem_consulta=None,
                classificacao="Sem consulta anterior identificada",
                possui_consulta_futura=bool(proxima_consulta),
                proxima_consulta=proxima_consulta,
            )

        dias_sem_consulta = (hoje - ultima_consulta).days

        if dias_sem_consulta <= 180:
            classificacao = "Acompanhamento recente: até 6 meses"
        elif dias_sem_consulta <= 365:
            classificacao = "Atenção: entre 6 e 12 meses"
        elif dias_sem_consulta <= 730:
            classificacao = "Atraso importante: entre 12 e 24 meses"
        else:
            classificacao = "Alto risco de perda de seguimento: mais de 24 meses"

        return MaintenanceResult(
            ultima_consulta=ultima_consulta,
            dias_sem_consulta=dias_sem_consulta,
            classificacao=classificacao,
            possui_consulta_futura=bool(proxima_consulta),
            proxima_consulta=proxima_consulta,
        )
from typing import List

from models.appointment import Appointment


class AppointmentService:
    """Converte os agendamentos da Clinicorp para objetos Appointment."""

    @staticmethod
    def converter(lista_json: list) -> List[Appointment]:
        agendamentos = []

        for item in lista_json:

            if not isinstance(item, dict):
                continue

            agendamento = Appointment(
                data=str(
                    item.get("Date")
                    or item.get("AtomicDate")
                    or item.get("date")
                    or ""
                ),

                hora_inicio=str(
                    item.get("StartTime")
                    or item.get("fromTime")
                    or ""
                ),

                hora_fim=str(
                    item.get("EndTime")
                    or item.get("toTime")
                    or ""
                ),

                paciente=str(
                    item.get("PatientName")
                    or item.get("Name")
                    or ""
                ),

                status=str(item.get("Status", "")),
                profissional=str(item.get("ProfessionalName", "")),
                observacao=str(item.get("Observation", "")),
            )

            agendamentos.append(agendamento)

        return agendamentos
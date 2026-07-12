from typing import List

from models.patient import Patient


class PatientService:

    @staticmethod
    def converter(lista_json: list) -> List[Patient]:

        pacientes = []

        for item in lista_json:

            paciente = Patient(
                id=item.get("PatientId"),
                nome=item.get("Name"),
                telefone=item.get("Phone"),
                email=item.get("Email"),
                status=item.get("Status"),
                data_nascimento=item.get("BirthDate"),
            )

            pacientes.append(paciente)

        return pacientes
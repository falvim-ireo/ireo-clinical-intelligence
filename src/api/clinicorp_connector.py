"""
IREO Clinical Intelligence
Clinicorp API Connector

Responsável pela comunicação com a API da Clinicorp.
"""

from typing import Any
import requests
from requests.auth import HTTPBasicAuth

from core.config import Config


class ClinicorpAPI:
    """Cliente para consultas somente leitura na API da Clinicorp."""

    def __init__(self) -> None:
        self.subscriber_id = Config.CLINICORP_SUBSCRIBER_ID
        self.business_id = Config.CLINICORP_BUSINESS_ID
        self.api_user = Config.CLINICORP_API_USER
        self.token = Config.CLINICORP_TOKEN
        self.base_url = Config.CLINICORP_BASE_URL

        self._validar_configuracao()

        self.auth = HTTPBasicAuth(
            self.api_user,
            self.token,
        )

        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        print("Clinicorp Connector inicializado.")

    def _validar_configuracao(self) -> None:
        """Confirma se as variáveis obrigatórias estão no arquivo .env."""

        variaveis_ausentes = []

        if not self.subscriber_id:
            variaveis_ausentes.append("CLINICORP_SUBSCRIBER_ID")

        if not self.api_user:
            variaveis_ausentes.append("CLINICORP_API_USER")

        if not self.token:
            variaveis_ausentes.append("CLINICORP_TOKEN")

        if not self.base_url:
            variaveis_ausentes.append("CLINICORP_BASE_URL")

        if variaveis_ausentes:
            nomes = ", ".join(variaveis_ausentes)
            raise ValueError(
                f"Variáveis ausentes no arquivo .env: {nomes}"
            )

    def _get(
        self,
        caminho: str,
        params: dict[str, Any],
    ) -> Any:
        """Executa uma requisição GET autenticada."""

        endpoint = f"{self.base_url}/{caminho.lstrip('/')}"

        try:
            response = requests.get(
                endpoint,
                auth=self.auth,
                headers=self.headers,
                params=params,
                timeout=30,
            )

            response.raise_for_status()

        except requests.exceptions.Timeout:
            raise RuntimeError(
                "A Clinicorp demorou mais de 30 segundos para responder."
            ) from None

        except requests.exceptions.HTTPError:
            raise RuntimeError(
                "A Clinicorp retornou um erro HTTP."
            ) from None

        except requests.exceptions.RequestException:
            raise RuntimeError(
                "Não foi possível conectar à Clinicorp."
            ) from None

        try:
            return response.json()

        except ValueError:
            raise RuntimeError(
                "A Clinicorp retornou uma resposta que não é JSON."
            ) from None

    def buscar_paciente(
        self,
        nome: str,
        somente_ativos: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Busca pacientes pelo nome completo.

        Por padrão, exclui registros vazios e cadastros com status DELETED.
        """

        dados = self._get(
            "patient/get",
            {
                "subscriber_id": self.subscriber_id,
                "Name": nome,
            },
        )

        if isinstance(dados, dict):
            dados = [dados]

        if not isinstance(dados, list):
            raise RuntimeError(
                "Formato inesperado na resposta da busca de pacientes."
            )

        pacientes = [
            paciente
            for paciente in dados
            if isinstance(paciente, dict) and paciente
        ]

        if somente_ativos:
            pacientes = [
                paciente
                for paciente in pacientes
                if str(paciente.get("Status", "")).upper() == "ACTIVE"
            ]

        return pacientes

    def listar_agendamentos(
        self,
        patient_id: int | str,
    ) -> list[dict[str, Any]]:
        """Retorna os agendamentos vinculados ao paciente."""

        dados = self._get(
            "patient/list_appointments",
            {
                "PatientId": patient_id,
            },
        )

        if isinstance(dados, dict):
            dados = [dados]

        if not isinstance(dados, list):
            raise RuntimeError(
                "Formato inesperado na resposta dos agendamentos."
            )

        return [
            agendamento
            for agendamento in dados
            if isinstance(agendamento, dict) and agendamento
        ]

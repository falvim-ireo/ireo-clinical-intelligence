from datetime import datetime
from typing import Any


def formatar_data(valor: Any) -> str:
    """Converte datas da API para o formato brasileiro."""

    if not valor:
        return "Não informada"

    texto = str(valor)

    formatos = (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    )

    for formato in formatos:
        try:
            data = datetime.strptime(texto, formato)
            return data.strftime("%d/%m/%Y")
        except ValueError:
            continue

    return texto


def formatar_horario(valor: Any) -> str:
    """Apresenta horários de forma padronizada."""

    if not valor:
        return "Não informado"

    texto = str(valor).strip()

    if len(texto) >= 5:
        return texto[:5]

    return texto


def valor_ou_padrao(
    valor: Any,
    padrao: str = "Não informado",
) -> str:
    """Evita campos vazios na apresentação."""

    if valor is None:
        return padrao

    texto = str(valor).strip()

    if not texto:
        return padrao

    return texto
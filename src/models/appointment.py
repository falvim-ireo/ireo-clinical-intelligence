from dataclasses import dataclass
from typing import Optional


@dataclass
class Appointment:
    data: str
    hora_inicio: str
    hora_fim: str
    paciente: str
    status: Optional[str] = None
    profissional: Optional[str] = None
    observacao: Optional[str] = None
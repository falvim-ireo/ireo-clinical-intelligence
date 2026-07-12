from dataclasses import dataclass
from typing import Optional


@dataclass
class Patient:
    id: int
    nome: str
    telefone: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    data_nascimento: Optional[str] = None
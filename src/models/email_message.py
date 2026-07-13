"""Modelo de uma mensagem de e-mail já carregada em memória."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class EmailMessage:
    """Dados necessários para planejar um intake sem acessar o provedor."""

    message_id: str
    subject: str
    sender: str
    reply_to: Optional[str]
    recipients: list[str] = field(default_factory=list)
    received_at: datetime = field(default_factory=datetime.now)
    text_body: str = ""
    html_body: str = ""

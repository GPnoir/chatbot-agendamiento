"""Interfaz base para canales de mensajería."""
from abc import ABC, abstractmethod


class BaseChannel(ABC):
    @abstractmethod
    async def send_message(self, recipient_id: str, text: str):
        pass

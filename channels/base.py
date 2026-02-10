"""Abstract base class for input channels."""
from abc import ABC, abstractmethod


class Channel(ABC):
    """Base class for all input channels (Telegram, Email, CLI)."""

    @abstractmethod
    async def start(self):
        """Start the channel."""

    @abstractmethod
    async def stop(self):
        """Stop the channel gracefully."""

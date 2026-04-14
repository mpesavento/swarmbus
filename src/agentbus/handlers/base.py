from abc import ABC, abstractmethod
from ..message import AgentMessage


class BaseHandler(ABC):
    """Receives messages dispatched by AgentBus.listen()."""

    @abstractmethod
    async def handle(self, msg: AgentMessage) -> None:
        """Process a received message. Exceptions are caught by the bus."""
        ...

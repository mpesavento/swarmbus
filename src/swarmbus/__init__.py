# src/swarmbus/__init__.py
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("swarmbus-py")
except PackageNotFoundError:
    __version__ = "unknown"

from .bus import AgentBus
from .message import AgentMessage
from .handlers.base import BaseHandler
from .handlers.file_bridge import FileBridgeHandler
from .handlers.direct_invoke import DirectInvocationHandler
from .handlers.persistent import PersistentListenerHandler
from .archive import SQLiteArchive

__all__ = [
    "AgentBus",
    "AgentMessage",
    "BaseHandler",
    "FileBridgeHandler",
    "DirectInvocationHandler",
    "PersistentListenerHandler",
    "SQLiteArchive",
]

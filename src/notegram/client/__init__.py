"""Dependency-free client. Importing this package never imports the server."""

from .config import ClientConfig
from .handler import NotegramHandler

__all__ = ["ClientConfig", "NotegramHandler"]

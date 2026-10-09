"""Shared TTS server. Importing this package never imports model frameworks."""

from .adapter import Adapter, AudioOutput, ExecutionContext, Profile
from .errors import DomainError

__all__ = ["Adapter", "AudioOutput", "ExecutionContext", "Profile", "DomainError"]
__version__ = "0.1.0"

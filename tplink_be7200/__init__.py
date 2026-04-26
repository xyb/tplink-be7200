"""Python SDK for the TP-Link BE7200 (TL-7DR7270) Web UI API.

Tested against firmware 1.0.18; see the repository README for the
reverse-engineered protocol notes.
"""

from . import cache
from .api import API, ApiError
from .auth import encrypt_password, get_encrypt_info, login

__all__ = ["API", "ApiError", "login", "get_encrypt_info", "encrypt_password", "cache"]
__version__ = "0.1.0"

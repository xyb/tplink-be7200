"""Python SDK for the TP-Link BE7200 (TL-7DR7270) Web UI API.

Tested against firmware 1.0.18; see the repository README for the
reverse-engineered protocol notes. As of the c6u migration the auth +
read paths are upstream `tplinkrouterc6u.TPLinkXDRClient`; this package
adds the extended write surface and the local stok cache.
"""

from . import cache
from .client import BE7200ApiError, BE7200Client

__all__ = ["BE7200Client", "BE7200ApiError", "cache"]
__version__ = "0.2.0"

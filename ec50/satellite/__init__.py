"""Companion Satellite integration for the EC-50."""

from .client import SatelliteClient
from .protocol import DEFAULT_PORT
from .service import SatelliteService
from .surfaces import build, check

__all__ = ["SatelliteClient", "SatelliteService", "DEFAULT_PORT", "build", "check"]

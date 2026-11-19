"""Pluggable Listener and Speaker adapters for Boss Rig Shared Plane."""

from plane.adapters.listeners import BaseListener, GoldfishChatListener, JsonlBusListener
from plane.adapters.speaker import (
    BaseSpeaker,
    CompositeSpeaker,
    ReportFileSpeaker,
    ToastNotificationSpeaker,
    read_latest_report,
)

__all__ = [
    "BaseListener",
    "BaseSpeaker",
    "CompositeSpeaker",
    "GoldfishChatListener",
    "JsonlBusListener",
    "ReportFileSpeaker",
    "ToastNotificationSpeaker",
    "read_latest_report",
]

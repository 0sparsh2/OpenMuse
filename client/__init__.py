"""Phase 9 — reference client for the OpenMuse platform.

Python client API layer over the External API contracts (api/server.py),
plus per-surface wrappers for the backend services (memory, scheduler,
connectors, browser, goals/feed/ideas). Local state holds only ephemeral
UI state; durable state lives on the server and is refetched or streamed.

The static web UI in client/web/ is built on the same contracts over
fetch/EventSource; see js/openmuse-api.js.
"""

from .api_client import OpenMuseClient, QueuedSend, SendResult
from .sse import SSEEvent, SSEClient, stream_events
from .http import HttpError, api_request
from .state import ClientState
from .approvals import ApprovalCard, DeviceAuth, TestDeviceAuth, HIGH_RISK_CLASSES

__all__ = [
    "OpenMuseClient", "QueuedSend", "SendResult",
    "SSEEvent", "SSEClient", "stream_events",
    "HttpError", "api_request",
    "ClientState",
    "ApprovalCard", "DeviceAuth", "TestDeviceAuth", "HIGH_RISK_CLASSES",
]

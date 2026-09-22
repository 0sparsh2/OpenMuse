"""Connectors — Phase 6.

An authenticated adapter to an external service. A connector owns OAuth,
pagination, provider errors, rate behavior, and webhook verification. It does
not own conversational policy.

Security invariants (blueprint "Connectors and secrets"):
  - The model never receives raw access or refresh tokens. Tools receive
    opaque connection ids; the registry resolves a short-lived credential
    handle internally, scoped to one call.
  - Secrets never enter model messages, memory, traces, artifact files,
    URLs, or queue payloads. The vault scans adapter output for leaks and
    fails closed.
  - Scope expansion requires a new consent ceremony.
  - A terminal provider limit stops further calls for that account/run.
  - Disconnect revokes provider tokens where supported and deletes vault
    material.
"""
from .models import (
    ConnectionRecord,
    ConnectorManifest,
    OperationDef,
    ProviderError,
    ProviderErrorCode,
    ConnectorError,
)
from .vault import Vault, MemoryVault
from .registry import ConnectorRegistry, ConnectorHandle
from .rest import RESTConnector, Transport, MockTransport
from .oauth import OAuthFlow, AuthorizationRequest
from .one_time_code import OneTimeCodeService

__all__ = [
    "ConnectionRecord",
    "ConnectorManifest",
    "OperationDef",
    "ProviderError",
    "ProviderErrorCode",
    "ConnectorError",
    "Vault",
    "MemoryVault",
    "ConnectorRegistry",
    "ConnectorHandle",
    "RESTConnector",
    "Transport",
    "MockTransport",
    "OAuthFlow",
    "AuthorizationRequest",
    "OneTimeCodeService",
]

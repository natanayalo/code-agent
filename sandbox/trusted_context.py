"""Trusted execution context for native agent sandbox boundaries.

This module defines the non-serializable, broker-authoritative context that must
be provided to the sandbox executor. It is explicitly separated from general task
DTOs to ensure it cannot leak into persistence, traces, or unhardened paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sandbox.capability import SandboxCapabilityGrant
from sandbox.provider_bootstrap import ProviderBootstrap
from sandbox.secrets import SecretResolver


@dataclass(frozen=True)
class TrustedSandboxExecutionContext:
    """
    Broker-authoritative execution context for a sandbox run.

    This object is intentionally not a Pydantic model and is never serialized.
    It contains the validated capability grant and the resolver capable of fetching
    raw ephemeral secret values.
    """

    grant: SandboxCapabilityGrant
    task_id: str
    secret_resolver: SecretResolver = field(repr=False)
    provider_bootstrap: ProviderBootstrap = field(repr=False)

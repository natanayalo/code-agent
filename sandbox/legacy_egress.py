"""Reuse native task-private proxy networking for legacy shell execution."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sandbox.native_agent_executor import DEFAULT_NATIVE_AGENT_IMAGE, DockerNativeAgentExecutor

logger = logging.getLogger(__name__)
_SAFE_NETWORK_ENV = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "PYTHONUNBUFFERED", "NO_COLOR"}
)


@dataclass
class LegacyEgress:
    """Names refer only to resources created for this invocation."""

    executor: DockerNativeAgentExecutor
    network: str
    proxy: str

    @classmethod
    def start(
        cls,
        *,
        environment: Mapping[str, str],
        artifact_root: Path,
        task_id: str,
        docker_binary: str,
    ) -> LegacyEgress:
        # Legacy DTOs cannot express credential audiences. Never combine arbitrary
        # sandbox credentials with public egress; use native grants for that flow.
        if set(environment) - _SAFE_NETWORK_ENV:
            raise ValueError(
                "Networked legacy execution cannot receive unscoped environment values"
            )
        executor = DockerNativeAgentExecutor(
            image=os.environ.get("CODE_AGENT_SANDBOX_IMAGE", DEFAULT_NATIVE_AGENT_IMAGE),
            docker_binary=docker_binary,
        )
        network = f"legacy-agent-net-{uuid4().hex[:20]}"
        artifact_root.mkdir(parents=True, exist_ok=True)
        proxy = executor._start_proxy(
            network_name=network,
            artifact_root=artifact_root,
            task_id=task_id,
            network_policy="public_https_proxy",
            allowed_hosts=[],
        )
        logger.info("Started task-private legacy egress", extra={"task_id": task_id})
        return cls(executor, network, proxy)

    def close(self) -> None:
        result = self.executor._cleanup_network(network_name=self.network, proxy_name=self.proxy)
        if result != "removed":
            logger.warning("Incomplete legacy egress cleanup", extra={"network": self.network})


def legacy_proxy_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        **environment,
        "HTTP_PROXY": "http://native-egress-proxy:8080",
        "HTTPS_PROXY": "http://native-egress-proxy:8080",
        "NO_PROXY": "",
    }

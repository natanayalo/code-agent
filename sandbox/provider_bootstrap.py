"""Provider bootstrap loader for native agent sandbox context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sandbox.provider_hosts import CODEX_RUNTIME_HOSTS, GEMINI_RUNTIME_HOSTS
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretScope,
    SecretSource,
)


class ProviderBootstrapError(Exception):
    """Raised when a trusted provider directory is missing required bootstrap files."""


@dataclass(frozen=True)
class ProviderBootstrap:
    """Bootstrapped configuration for a native provider execution."""

    definitions: list[RegisteredSecretDefinition]
    file_store: dict[str, str] = field(repr=False)
    destination_by_ref: dict[str, str]
    ref_names: tuple[str, ...]


class ProviderBootstrapLoader:
    """Loads a provider directory and prepares registered secrets."""

    # Map of known files to (is_required, ref_name, logical_mount_path, provider_hosts)
    # Using 'codex' and 'gemini' as top-level dir inference.
    _FILE_POLICIES: Final[dict[str, tuple[bool, str, str, tuple[str, ...]]]] = {
        "auth.json": (True, "codex_auth_json", ".codex/auth.json", CODEX_RUNTIME_HOSTS),
        "config.toml": (False, "codex_config_toml", ".codex/config.toml", CODEX_RUNTIME_HOSTS),
        "oauth_creds.json": (
            True,
            "gemini_oauth_creds",
            ".gemini/oauth_creds.json",
            GEMINI_RUNTIME_HOSTS,
        ),
        "settings.json": (False, "gemini_settings", ".gemini/settings.json", GEMINI_RUNTIME_HOSTS),
    }

    @classmethod
    def load(cls, provider_dir: Path) -> ProviderBootstrap:
        """Load bootstrap definitions from a provider config directory."""
        definitions: list[RegisteredSecretDefinition] = []
        file_store: dict[str, str] = {}
        destination_by_ref: dict[str, str] = {}
        ref_names: list[str] = []

        is_gemini = provider_dir.name == ".gemini"
        is_codex = provider_dir.name == ".codex"

        found_required = False

        if not provider_dir.exists() or not provider_dir.is_dir():
            pass  # We'll let the require-checks at the end handle it
        else:
            for file_path in provider_dir.iterdir():
                if not file_path.is_file():
                    continue

                file_name = file_path.name
                if file_name not in cls._FILE_POLICIES:
                    continue

                is_required, ref_name, dest_path, runtime_hosts = cls._FILE_POLICIES[file_name]

                # Only process files corresponding to the expected provider type
                if is_gemini and "gemini" not in ref_name:
                    continue
                if is_codex and "codex" not in ref_name:
                    continue

                if is_required:
                    found_required = True

                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception as e:
                    if is_required:
                        raise ProviderBootstrapError(
                            f"Failed to read required bootstrap file {file_path}: {e}"
                        )
                    continue

                file_store[ref_name] = content
                destination_by_ref[ref_name] = dest_path
                ref_names.append(ref_name)

                definition = RegisteredSecretDefinition(
                    name=ref_name,
                    source=SecretSource.FILE,
                    source_key=ref_name,
                    required_scope=SecretScope.PROVIDER_AUTH,
                    exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
                    permitted_egress_hosts=runtime_hosts,
                    destination_mount_path=ref_name,  # Mapped to ref_name logically
                )
                definitions.append(definition)

        if not found_required and is_codex and not provider_dir.joinpath("auth.json").exists():
            raise ProviderBootstrapError(f"Required auth.json missing in {provider_dir}")
        if (
            not found_required
            and is_gemini
            and not provider_dir.joinpath("oauth_creds.json").exists()
        ):
            raise ProviderBootstrapError(f"Required oauth_creds.json missing in {provider_dir}")

        return ProviderBootstrap(
            definitions=definitions,
            file_store=file_store,
            destination_by_ref=destination_by_ref,
            ref_names=tuple(ref_names),
        )

"""Provider bootstrap loader for native agent sandbox context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sandbox.provider_hosts import (
    CODEX_API_KEY_HOSTS,
    CODEX_CHATGPT_HOSTS,
    GEMINI_API_KEY_HOSTS,
    GEMINI_OAUTH_HOSTS,
)
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

    @classmethod
    def load(cls, provider_dir: Path, has_api_key: bool = False) -> ProviderBootstrap:
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

                # Determine policy based on auth mode
                is_required = False
                ref_name = ""
                dest_path = ""
                runtime_hosts: tuple[str, ...] = ()

                if file_name == "auth.json" and is_codex:
                    if has_api_key:
                        continue
                    is_required = not has_api_key
                    ref_name = "codex_auth_json"
                    dest_path = ".codex/auth.json"
                    runtime_hosts = CODEX_CHATGPT_HOSTS
                elif file_name == "config.toml" and is_codex:
                    is_required = False
                    ref_name = "codex_config_toml"
                    dest_path = ".codex/config.toml"
                    runtime_hosts = CODEX_CHATGPT_HOSTS if not has_api_key else CODEX_API_KEY_HOSTS
                elif file_name == "oauth_creds.json" and is_gemini:
                    if has_api_key:
                        continue
                    is_required = not has_api_key
                    ref_name = "gemini_oauth_creds"
                    dest_path = ".gemini/oauth_creds.json"
                    runtime_hosts = GEMINI_OAUTH_HOSTS
                elif file_name == "settings.json" and is_gemini:
                    is_required = False
                    ref_name = "gemini_settings"
                    dest_path = ".gemini/settings.json"
                    runtime_hosts = GEMINI_OAUTH_HOSTS if not has_api_key else GEMINI_API_KEY_HOSTS
                else:
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

        if not has_api_key:
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

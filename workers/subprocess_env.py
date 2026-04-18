"""Helpers for scoping subprocess environments to the minimum needed surface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_BASE_ALLOWED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "TZ",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_BASE_ALLOWED_ENV_PREFIXES = ("LC_",)

_CODEX_ALLOWED_ENV_KEYS = (
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
)
_GEMINI_ALLOWED_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def _build_scoped_env(
    environ: Mapping[str, str],
    *,
    allowed_keys: Sequence[str],
) -> dict[str, str]:
    """Filter a process env mapping to explicitly allowed keys/prefixes."""
    allowlist = _BASE_ALLOWED_ENV_KEYS.union(allowed_keys)
    scoped: dict[str, str] = {}
    # Snapshot items to avoid RuntimeError if os.environ mutates concurrently.
    for key, value in list(environ.items()):
        if key in allowlist or key.startswith(_BASE_ALLOWED_ENV_PREFIXES):
            scoped[key] = value
    return scoped


def build_codex_subprocess_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Build a minimal subprocess environment for Codex CLI invocation."""
    return _build_scoped_env(environ, allowed_keys=_CODEX_ALLOWED_ENV_KEYS)


def build_gemini_subprocess_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Build a minimal subprocess environment for Gemini CLI invocation."""
    return _build_scoped_env(environ, allowed_keys=_GEMINI_ALLOWED_ENV_KEYS)

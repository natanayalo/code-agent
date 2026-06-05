"""Shared constants for the sandbox layer."""

from typing import Final

# --- Sandbox Command Limits ---
# This is the maximum ceiling for any single command run in the sandbox.
DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS: Final[int] = 300

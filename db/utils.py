"""Database-level utility functions."""

import hashlib
import json
from typing import Any


def compute_interaction_content_hash(
    interaction_type: str, summary: str, data: dict[str, Any] | None = None
) -> str:
    """Compute a stable content hash for an interaction requirement, ignoring volatile fields."""
    stable_data = {
        k: v
        for k, v in (data or {}).items()
        if k not in {"source", "resume_token", "created_at", "updated_at"}
    }
    payload = {
        "type": str(interaction_type),
        "summary": summary,
        "data": stable_data,
    }
    content = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()

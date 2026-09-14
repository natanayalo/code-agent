"""Versioned ContextEnvelope contracts and deterministic pre-dispatch assembler (M28.5D)."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import Field

from db.base import utc_now
from orchestrator.state import MemoryContext, OrchestratorModel, TaskSpec
from sandbox.redact import SecretRedactor, mask_url_credentials
from workers.base import WorkerRequest
from workers.prompt_workspace import _extract_front_matter_metadata

logger = logging.getLogger(__name__)

MAX_ENVELOPE_BYTES = 256 * 1024
MAX_OBJECTIVE_CHARS, MAX_REPO_INSTRUCTIONS_CHARS = 4000, 4000
MAX_LIST_ITEMS, MAX_STRING_ITEM_CHARS, MAX_SKILLS = 20, 500, 30
MAX_DEPENDENCY_OUTPUTS, MAX_DEPENDENCY_SUMMARY_CHARS, MAX_GATED_MEMORY_ENTRIES = 20, 2000, 50
MAX_BUILD_SYSTEMS, MAX_FILES_TOUCHED, MAX_SELECTED_REFERENCES = 10, 200, 200
FORBIDDEN_SECRET_KEYS = frozenset(
    {"password", "token", "secret", "api_key", "private_key", "credential"}
)


class RepoFacts(OrchestratorModel):
    repo_url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    worktree_state_digest: str | None = None
    workspace_mode: str | None = None
    workspace_id: str | None = None
    has_agents_md: bool | None = None
    detected_build_systems: list[str] = Field(default_factory=list, max_length=10)
    workspace_identity_omission_reason: str = "orchestrator_dispatch_boundary"


class RepoSkillRef(OrchestratorModel):
    name: str
    description: str | None = None
    relative_path: str


class DependencyOutput(OrchestratorModel):
    node_id: str
    summary: str
    status: str
    files_changed: list[str] = Field(default_factory=list, max_length=50)
    artifact_names: list[str] = Field(default_factory=list, max_length=20)


class GatedMemoryEntry(OrchestratorModel):
    memory_key: str
    value: dict[str, Any]
    category: Literal["personal", "project"]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
    staleness: float = 0.0
    conflict: str | None = None
    risk: str = "low"
    advisory_strength: float = 1.0
    gate_status: str = "accepted"
    gate_reason_codes: list[str] = Field(default_factory=list)


class SessionContext(OrchestratorModel):
    active_goal: str | None = None
    decisions_made: dict[str, Any] = Field(default_factory=dict)
    identified_risks: dict[str, Any] = Field(default_factory=dict)
    files_touched: list[str] = Field(default_factory=list)


class CapabilitySummary(OrchestratorModel):
    read_only: bool = False
    risk_level: str = "low"
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    delivery_mode: str = "workspace"
    network_enabled: bool = False
    granted_secret_refs: list[str] = Field(default_factory=list)
    worker_type: str | None = None
    worker_profile: str | None = None
    runtime_mode: str | None = None


class SelectedReference(OrchestratorModel):
    path: str
    source: Literal["session_files_touched", "dependency_files_changed", "memory_value"]


class TruncationRecord(OrchestratorModel):
    field: str
    original_length: int
    truncated_length: int
    reason: str = "exceeded_limit"


class ContextEnvelope(OrchestratorModel):
    schema_version: Literal[1] = 1
    envelope_id: str
    task_id: str
    session_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    dispatch_role: Literal["primary", "decomposed_node"] = "primary"
    logical_attempt: int = 1
    assembled_at: datetime
    context_content_digest: str
    evidence_digest: str
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    repo_facts: RepoFacts = Field(default_factory=RepoFacts)
    repo_instructions_snapshot: str | None = None
    repo_skills: list[RepoSkillRef] = Field(default_factory=list)
    dependency_outputs: list[DependencyOutput] = Field(default_factory=list)
    gated_memory_entries: list[GatedMemoryEntry] = Field(default_factory=list)
    gate_diagnostics_summary: dict[str, Any] = Field(default_factory=dict)
    session_context: SessionContext = Field(default_factory=SessionContext)
    capability_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    selected_references: list[SelectedReference] = Field(default_factory=list)
    truncations: list[TruncationRecord] = Field(default_factory=list)


def _add_trunc(
    tr: list[TruncationRecord] | None, f: str, orig: int, trunc: int, r: str = "exceeded_limit"
) -> None:
    if tr is not None and f:
        tr.append(TruncationRecord(field=f, original_length=orig, truncated_length=trunc, reason=r))


def _bound_str(
    text: str,
    max_chars: int,
    field: str = "",
    truncations: list[TruncationRecord] | None = None,
) -> str:
    if len(text) <= max_chars:
        return text
    _add_trunc(truncations, field, len(text), max_chars)
    cut = max(0, max_chars - len("\n... (truncated)"))
    return text[:cut] + "\n... (truncated)"


def _bound_string_list(
    items: list[str],
    max_items: int,
    max_chars: int,
    field: str = "",
    truncations: list[TruncationRecord] | None = None,
) -> list[str]:
    if len(items) > max_items:
        _add_trunc(truncations, f"{field}_count", len(items), max_items)
    out: list[str] = []
    for idx, s in enumerate(items[:max_items]):
        if len(s) > max_chars:
            _add_trunc(truncations, f"{field}[{idx}]", len(s), max_chars)
            out.append(s[:max_chars])
        else:
            out.append(s)
    return out


def _bound_json_value(
    value: Any,
    max_str: int = 2000,
    max_list: int = 50,
    max_keys: int = 50,
    max_depth: int = 5,
    depth: int = 0,
    field: str = "",
    truncations: list[TruncationRecord] | None = None,
) -> Any:
    if depth >= max_depth:
        _add_trunc(truncations, field, depth, max_depth, "max_depth_exceeded")
        return "<nested_depth_exceeded>"
    if isinstance(value, str):
        if len(value) > max_str:
            _add_trunc(truncations, field, len(value), max_str)
            return value[:max_str]
        return value
    if isinstance(value, list):
        if len(value) > max_list:
            _add_trunc(truncations, f"{field}_count", len(value), max_list)
        return [
            _bound_json_value(
                x,
                max_str,
                max_list,
                max_keys,
                max_depth,
                depth + 1,
                f"{field}[{i}]" if field else "",
                truncations,
            )
            for i, x in enumerate(value[:max_list])
        ]
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value.keys())
        if len(keys) > max_keys:
            _add_trunc(truncations, f"{field}_keys", len(keys), max_keys)
        return {
            k: _bound_json_value(
                value[k],
                max_str,
                max_list,
                max_keys,
                max_depth,
                depth + 1,
                f"{field}.{k}" if field else str(k),
                truncations,
            )
            for k in keys[:max_keys]
        }
    return value


def _safe_read_workspace_file(
    workspace_path: Path | None,
    file_path: Path,
    max_chars: int,
) -> tuple[str | None, int, bool]:
    """Read bounded text from workspace file, rejecting symlinks and path escapes.

    Returns:
        tuple of (content[:max_chars], orig_length, has_overflow)
    """
    if workspace_path is None or not workspace_path.exists():
        return None, 0, False
    try:
        ws_resolved = workspace_path.resolve()
        if file_path.is_symlink():
            logger.warning("Rejected symlink repository file %s", file_path)
            return None, 0, False
        file_resolved = file_path.resolve()
        if not file_resolved.is_relative_to(ws_resolved):
            logger.warning("Rejected path escaping workspace boundary: %s", file_path)
            return None, 0, False
        if file_resolved.is_symlink() or not file_resolved.is_file():
            return None, 0, False
        st_size = file_resolved.stat().st_size
        with file_resolved.open("r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(max_chars + 1)
        has_overflow = len(chunk) > max_chars
        content = chunk[:max_chars]
        orig_length = max(st_size, len(chunk)) if has_overflow else len(chunk)
        return content, orig_length, has_overflow
    except (ValueError, OSError) as exc:
        logger.warning("Failed to safely read workspace file %s: %s", file_path, exc)
        return None, 0, False


def _find_git_dir(
    workspace_path: Path | None,
    trusted_git_dir: Path | None = None,
    workspace_id: str | None = None,
) -> Path | None:
    """Resolve trusted or workspace git directory, rejecting symlinks."""
    target = trusted_git_dir
    if target is None and workspace_path is not None and workspace_id:
        candidate = workspace_path.parent / ".code-agent-git" / workspace_id
        if candidate.is_dir() and not candidate.is_symlink():
            target = candidate
    if target is None and workspace_path is not None and workspace_path.exists():
        candidate_ws_git = workspace_path / ".git"
        if candidate_ws_git.exists() and not candidate_ws_git.is_symlink():
            target = candidate_ws_git
    if target is None or not target.exists() or target.is_symlink():
        return None
    return target


def _resolve_git_commit_sha(
    workspace_path: Path | None,
    trusted_git_dir: Path | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Resolve Git commit SHA, prioritizing broker-authoritative trusted Git dir."""
    target_git_dir = _find_git_dir(workspace_path, trusted_git_dir, workspace_id)
    if target_git_dir is None:
        return None

    try:
        proc = subprocess.run(
            ["git", "--git-dir", str(target_git_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            if len(sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in sha):
                return sha.lower()
    except Exception:
        logger.debug("Failed to run git rev-parse for %s", target_git_dir, exc_info=True)

    try:
        head_file = target_git_dir / "HEAD" if target_git_dir.is_dir() else None
        if head_file and head_file.is_file() and not head_file.is_symlink():
            content = head_file.read_text(encoding="utf-8").strip()
            if len(content) == 40 and all(c in "0123456789abcdefABCDEF" for c in content):
                return content.lower()
            if content.startswith("ref: "):
                ref_target = target_git_dir / content[5:].strip()
                if ref_target.is_file() and not ref_target.is_symlink():
                    sha = ref_target.read_text(encoding="utf-8").strip()
                    if len(sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in sha):
                        return sha.lower()
    except Exception:
        pass
    return None


def _stream_git_diff(target_git_dir: Path, workspace_path: Path) -> tuple[bytes | None, int]:
    """Stream git diff HEAD into a SHA-256 digest without unbounded memory buffering."""
    cmd = [
        "git",
        "--git-dir",
        str(target_git_dir),
        "--work-tree",
        str(workspace_path),
        "diff",
        "HEAD",
    ]
    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            diff_hasher = hashlib.sha256()
            diff_bytes_count = 0
            assert proc.stdout is not None
            while chunk := proc.stdout.read(65536):
                diff_hasher.update(chunk)
                diff_bytes_count += len(chunk)
            _, stderr_bytes = proc.communicate(timeout=10)
            if proc.returncode != 0:
                logger.warning(
                    "git diff HEAD failed with code %s: %s",
                    proc.returncode,
                    stderr_bytes.decode("utf-8", errors="replace").strip(),
                )
                return None, 0
            return diff_hasher.digest(), diff_bytes_count
    except Exception:
        logger.warning("git diff HEAD execution failed", exc_info=True)
        return None, 0


def _get_git_status_lines(target_git_dir: Path, workspace_path: Path) -> list[str] | None:
    """Run git status --porcelain=v1 -uall. Returns lines or None on failure."""
    cmd = [
        "git",
        "--git-dir",
        str(target_git_dir),
        "--work-tree",
        str(workspace_path),
        "status",
        "--porcelain=v1",
        "-uall",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            logger.warning(
                "git status failed with code %s: %s",
                proc.returncode,
                proc.stderr.strip() if proc.stderr else "",
            )
            return None
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        logger.warning("git status execution failed", exc_info=True)
        return None


def _stream_hash_file(target: Path, ws_resolved: Path, hasher: Any) -> bool:
    """Stream file bytes into hasher if within workspace and not a symlink."""
    if target.is_symlink():
        logger.warning("Rejecting symlinked file in worktree digest: %s", target)
        return False
    try:
        resolved = target.resolve()
    except OSError:
        logger.warning("Failed to resolve file path: %s", target)
        return False
    if not resolved.is_relative_to(ws_resolved):
        logger.warning("Rejecting out-of-workspace file in worktree digest: %s", target)
        return False
    if not resolved.is_file():
        return True
    try:
        file_size = resolved.stat().st_size
        hasher.update(f"SIZE:{file_size}\nDATA:".encode("ascii"))
        with resolved.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        hasher.update(b"\n")
        return True
    except OSError:
        logger.warning("Failed reading file for worktree digest: %s", resolved, exc_info=True)
        return False


def _compute_worktree_state_digest(
    workspace_path: Path | None,
    trusted_git_dir: Path | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Compute deterministic SHA-256 over tracked and untracked workspace changes."""
    if workspace_path is None or not workspace_path.exists():
        return None
    target_git_dir = _find_git_dir(workspace_path, trusted_git_dir, workspace_id)
    if target_git_dir is None:
        return None

    diff_digest, diff_bytes = _stream_git_diff(target_git_dir, workspace_path)
    if diff_digest is None:
        return None

    status_lines = _get_git_status_lines(target_git_dir, workspace_path)
    if status_lines is None:
        return None

    if diff_bytes == 0 and not status_lines:
        return hashlib.sha256(b"clean").hexdigest()

    h = hashlib.sha256()
    h.update(b"---TRACKED_DIFF---\n")
    h.update(diff_digest)
    h.update(b"\n---STATUS---\n")
    ws_resolved = workspace_path.resolve()
    for line in sorted(status_lines):
        h.update(line.encode("utf-8") + b"\n")
        if line.startswith("?? "):
            rel = line[3:].strip()
            h.update(f"---UNTRACKED:{rel}\n".encode())
            if not _stream_hash_file(workspace_path / rel, ws_resolved, h):
                return None
            h.update(b"---END_UNTRACKED---\n")
    return h.hexdigest()


def _discover_repo_skills(
    workspace_path: Path | None, tr: list[TruncationRecord]
) -> list[RepoSkillRef]:
    if workspace_path is None or not workspace_path.exists():
        return []
    skills_dir = workspace_path / ".agents" / "skills"
    if not skills_dir.is_dir() or skills_dir.is_symlink():
        return []
    try:
        ws_resolved = workspace_path.resolve()
        if not skills_dir.resolve().is_relative_to(ws_resolved):
            return []
    except (ValueError, OSError):
        return []
    skills: list[RepoSkillRef] = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            raw, orig_len, has_overflow = _safe_read_workspace_file(
                workspace_path, skill_file, 8192
            )
            if not raw:
                continue
            skill_name = skill_file.parent.name
            if has_overflow:
                _add_trunc(tr, f"repo_skills[{skill_name}].source_file", orig_len, 8192)
            name, desc, _ = _extract_front_matter_metadata(raw)
            resolved_name = name or skill_name
            bound_desc = (
                _bound_str(desc, 200, f"repo_skills[{skill_name}].description", tr)
                if desc
                else None
            )
            skills.append(
                RepoSkillRef(
                    name=resolved_name,
                    description=bound_desc,
                    relative_path=f".agents/skills/{skill_name}/SKILL.md",
                )
            )
        except Exception:
            logger.debug("Failed to inspect skill file %s", skill_file, exc_info=True)
    if len(skills) > MAX_SKILLS:
        _add_trunc(tr, "repo_skills", len(skills), MAX_SKILLS)
        return skills[:MAX_SKILLS]
    return skills


_BUILD_MARKERS = (
    ("pyproject.toml", "poetry/pyproject"),
    ("Dockerfile", "docker"),
    (".github/workflows", "github_actions"),
    ("package.json", "npm"),
)


def _detect_build_systems(workspace_path: Path | None) -> list[str]:
    if workspace_path is None or not workspace_path.exists():
        return []
    return sorted(lbl for p, lbl in _BUILD_MARKERS if (workspace_path / p).exists())[
        :MAX_BUILD_SYSTEMS
    ]


_SCHEMA_ALLOWLIST_KEYS = frozenset({"granted_secret_refs"})


def _is_credential_key(key: str) -> bool:
    lk = str(key).lower().replace("-", "_")
    if lk in _SCHEMA_ALLOWLIST_KEYS:
        return False
    if lk in FORBIDDEN_SECRET_KEYS:
        return True
    return any(
        lk.endswith(f"_{fsk}") or lk.startswith(f"{fsk}_") or f"_{fsk}_" in lk
        for fsk in FORBIDDEN_SECRET_KEYS
    )


def _sanitize_dict(data: dict[str, Any], legacy_secrets: list[str]) -> dict[str, Any]:
    clean = [
        s
        for s in legacy_secrets
        if s and len(s.strip()) > 3 and s.strip().lower() not in {"true", "false", "none", "null"}
    ]
    redactor = SecretRedactor(clean) if clean else None

    def _scan(obj: Any) -> Any:
        if isinstance(obj, str):
            masked = mask_url_credentials(obj)
            return redactor.redact(masked) if redactor else masked
        if isinstance(obj, dict):
            res = {}
            for k, v in obj.items():
                if _is_credential_key(str(k)):
                    logger.warning("Redacting credential key %r in context envelope payload", k)
                    res[k] = "[REDACTED]"
                else:
                    res[k] = _scan(v)
            return res
        if isinstance(obj, list):
            return [_scan(item) for item in obj]
        return obj

    return _scan(data)


def _compute_canonical_bytes(
    data: dict[str, Any], excluded_keys: frozenset[str] | set[str]
) -> bytes:
    filtered = {k: v for k, v in data.items() if k not in excluded_keys}
    return json.dumps(
        filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


_VOLATILE_CONTEXT_KEYS = frozenset(
    {"envelope_id", "task_id", "session_id", "run_id"}
    | {"node_id", "logical_attempt", "assembled_at", "context_content_digest", "evidence_digest"}
)
_VOLATILE_EVIDENCE_KEYS = frozenset({"envelope_id", "assembled_at", "evidence_digest"})


def _compute_digests(data: dict[str, Any]) -> tuple[str, str]:
    ctx_data = dict(data)
    if isinstance(ctx_data.get("repo_facts"), dict):
        ctx_data["repo_facts"] = {
            k: v for k, v in ctx_data["repo_facts"].items() if k != "workspace_id"
        }
    content_digest = hashlib.sha256(
        _compute_canonical_bytes(ctx_data, _VOLATILE_CONTEXT_KEYS)
    ).hexdigest()

    ev_data = dict(data)
    ev_data["context_content_digest"] = content_digest
    evidence_digest = hashlib.sha256(
        _compute_canonical_bytes(ev_data, _VOLATILE_EVIDENCE_KEYS)
    ).hexdigest()

    return content_digest, evidence_digest


def _envelope_size(data: dict[str, Any], truncations: list[TruncationRecord]) -> int:
    data["truncations"] = [t.model_dump() for t in truncations]
    return len(json.dumps(data, default=str).encode("utf-8")) + 200


def _apply_progressive_truncation(data: dict[str, Any], tr: list[TruncationRecord]) -> bool:
    if _envelope_size(data, tr) <= MAX_ENVELOPE_BYTES:
        return True
    snap = data.get("repo_instructions_snapshot")
    if snap and len(snap) > 2000:
        _add_trunc(tr, "repo_instructions_snapshot_stage1", len(snap), 2000)
        data["repo_instructions_snapshot"] = snap[:2000] + "\n... (truncated)"
        if _envelope_size(data, tr) <= MAX_ENVELOPE_BYTES:
            return True
    mems = data.get("gated_memory_entries", [])
    if len(mems) > 25:
        _add_trunc(tr, "gated_memory_entries_stage2", len(mems), 25)
        data["gated_memory_entries"] = mems[:25]
        if _envelope_size(data, tr) <= MAX_ENVELOPE_BYTES:
            return True
    deps = data.get("dependency_outputs", [])
    for idx, dep in enumerate(deps):
        if isinstance(dep, dict) and len(dep.get("summary", "")) > 500:
            _add_trunc(tr, f"dependency_outputs[{idx}].summary_stage3", len(dep["summary"]), 500)
            dep["summary"] = dep["summary"][:500] + "... (truncated)"
    if deps and _envelope_size(data, tr) <= MAX_ENVELOPE_BYTES:
        return True
    if gd := data.get("gate_diagnostics_summary"):
        _add_trunc(tr, "gate_diagnostics_summary_stage4", len(gd), 0, "cleared_for_envelope_budget")
        data["gate_diagnostics_summary"] = {}
    return _envelope_size(data, tr) <= MAX_ENVELOPE_BYTES


def _extract_gated_memories(
    memory_context: MemoryContext | None, tr: list[TruncationRecord]
) -> list[dict[str, Any]]:
    if memory_context is None:
        return []
    entries = []
    for cat, items in (("personal", memory_context.personal), ("project", memory_context.project)):
        for item in items:
            if getattr(item, "gate_status", "accepted") == "suppressed":
                continue
            val = _bound_json_value(
                getattr(item, "value", {}),
                field=f"gated_memory[{item.memory_key}]",
                truncations=tr,
            )
            e = {
                "memory_key": item.memory_key,
                "category": cat,
                "value": val if isinstance(val, dict) else {"raw": val},
                "source": getattr(item, "source", None),
                "confidence": float(getattr(item, "confidence", 1.0)),
                "scope": getattr(item, "scope", None),
                "last_verified_at": getattr(item, "last_verified_at", None),
                "requires_verification": bool(getattr(item, "requires_verification", True)),
                "staleness": float(getattr(item, "staleness", 0.0)),
                "conflict": getattr(item, "conflict", None),
                "risk": str(getattr(item, "risk", "low")),
                "advisory_strength": float(getattr(item, "advisory_strength", 1.0)),
                "gate_status": str(getattr(item, "gate_status", "accepted")),
                "gate_reason_codes": list(getattr(item, "gate_reason_codes", [])),
            }
            entries.append(e)

    entries.sort(key=lambda e: float(cast(float, e.get("advisory_strength") or 0.0)), reverse=True)
    if len(entries) > MAX_GATED_MEMORY_ENTRIES:
        _add_trunc(tr, "gated_memory_entries", len(entries), MAX_GATED_MEMORY_ENTRIES)
        return entries[:MAX_GATED_MEMORY_ENTRIES]
    return entries


def _extract_dependencies(
    prior_node_context: dict[str, Any] | None, tr: list[TruncationRecord]
) -> list[dict[str, Any]]:
    if not prior_node_context:
        return []
    deps: list[dict[str, Any]] = []
    for node_id, payload in sorted(prior_node_context.items()):
        if not isinstance(payload, dict):
            continue
        prefix = f"dependency_outputs[{node_id}]"
        summary = _bound_str(
            str(payload.get("summary") or ""),
            MAX_DEPENDENCY_SUMMARY_CHARS,
            f"{prefix}.summary",
            tr,
        )
        rf = [str(f) for f in (payload.get("files_changed") or [])]
        ra = [
            str(a.get("name") if isinstance(a, dict) else a)
            for a in (payload.get("artifacts") or [])
        ]
        if len(rf) > 50:
            _add_trunc(tr, f"{prefix}.files_changed", len(rf), 50)
        if len(ra) > 20:
            _add_trunc(tr, f"{prefix}.artifact_names", len(ra), 20)
        deps.append(
            {
                "node_id": str(node_id),
                "summary": summary,
                "status": str(payload.get("status") or "completed"),
                "files_changed": rf[:50],
                "artifact_names": ra[:20],
            }
        )
    if len(deps) > MAX_DEPENDENCY_OUTPUTS:
        _add_trunc(tr, "dependency_outputs", len(deps), MAX_DEPENDENCY_OUTPUTS)
        return deps[:MAX_DEPENDENCY_OUTPUTS]
    return deps


def _project_intent_and_repo(
    task_spec: TaskSpec | None,
    task_text: str,
    worker_request: WorkerRequest,
    workspace_path: Path | None,
    truncations: list[TruncationRecord],
    trusted_git_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None, list[dict[str, Any]]]:
    raw_goal = task_spec.goal if task_spec and task_spec.goal else task_text
    intent: dict[str, Any] = {
        "objective": _bound_str(raw_goal, MAX_OBJECTIVE_CHARS, "objective", truncations)
    }
    for f_name, src in [
        ("acceptance_criteria", task_spec.acceptance_criteria if task_spec else []),
        ("assumptions", task_spec.assumptions if task_spec else []),
        ("non_goals", task_spec.non_goals if task_spec else []),
        ("verification_plan", task_spec.verification_commands if task_spec else []),
    ]:
        intent[f_name] = _bound_string_list(
            src, MAX_LIST_ITEMS, MAX_STRING_ITEM_CHARS, f_name, truncations
        )
    has_agents: bool | None = None
    if workspace_path and workspace_path.exists():
        agents_path = workspace_path / "AGENTS.md"
        try:
            ws_resolved = workspace_path.resolve()
            has_agents = (
                not agents_path.is_symlink()
                and agents_path.resolve().is_relative_to(ws_resolved)
                and agents_path.resolve().is_file()
            )
        except (ValueError, OSError):
            has_agents = False

    r_url = mask_url_credentials(worker_request.repo_url) if worker_request.repo_url else None
    repo_facts = {
        "repo_url": r_url,
        "branch": worker_request.branch,
        "commit_sha": _resolve_git_commit_sha(
            workspace_path,
            trusted_git_dir=trusted_git_dir,
            workspace_id=worker_request.workspace_id,
        ),
        "worktree_state_digest": _compute_worktree_state_digest(
            workspace_path,
            trusted_git_dir=trusted_git_dir,
            workspace_id=worker_request.workspace_id,
        ),
        "workspace_mode": task_spec.workspace_mode if task_spec else "clone",
        "workspace_id": worker_request.workspace_id,
        "has_agents_md": has_agents,
        "detected_build_systems": _detect_build_systems(workspace_path),
        "workspace_identity_omission_reason": "orchestrator_dispatch_boundary",
    }
    instructions: str | None = None
    if workspace_path and workspace_path.exists():
        raw_g, orig_length, has_overflow = _safe_read_workspace_file(
            workspace_path, workspace_path / "AGENTS.md", MAX_REPO_INSTRUCTIONS_CHARS
        )
        if raw_g:
            if has_overflow:
                _add_trunc(
                    truncations,
                    "repo_instructions_snapshot",
                    orig_length,
                    MAX_REPO_INSTRUCTIONS_CHARS,
                )
                cut = max(0, MAX_REPO_INSTRUCTIONS_CHARS - len("\n... (truncated)"))
                instructions = raw_g[:cut].rstrip() + "\n... (truncated)"
            else:
                instructions = raw_g.strip()
    skills = [s.model_dump() for s in _discover_repo_skills(workspace_path, truncations)]
    return intent, repo_facts, instructions, skills


def _project_context_sections(
    memory_context: MemoryContext | None,
    worker_request: WorkerRequest,
    task_spec: TaskSpec | None,
    deps: list[dict[str, Any]],
    tr: list[TruncationRecord],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    raw_session = (memory_context.session if memory_context else {}) or {}
    raw_files = list(raw_session.get("files_touched") or [])
    if len(raw_files) > MAX_FILES_TOUCHED:
        _add_trunc(tr, "session_context.files_touched", len(raw_files), MAX_FILES_TOUCHED)
    dm = _bound_json_value(
        raw_session.get("decisions_made") or {},
        field="session_context.decisions_made",
        truncations=tr,
    )
    ir = _bound_json_value(
        raw_session.get("identified_risks") or {},
        field="session_context.identified_risks",
        truncations=tr,
    )
    session_context = {
        "active_goal": raw_session.get("active_goal"),
        "decisions_made": dm,
        "identified_risks": ir,
        "files_touched": raw_files[:MAX_FILES_TOUCHED],
    }
    constraints = dict(worker_request.constraints or {})
    resolved_risk = (
        task_spec.risk_level
        if task_spec and getattr(task_spec, "risk_level", None)
        else constraints.get("risk_level", "low")
    )
    cap_summary = {
        "read_only": worker_request.read_only,
        "risk_level": str(resolved_risk),
        "allowed_actions": list(task_spec.allowed_actions if task_spec else []),
        "forbidden_actions": list(task_spec.forbidden_actions if task_spec else []),
        "delivery_mode": str(task_spec.delivery_mode if task_spec else "workspace"),
        "network_enabled": worker_request.network_enabled,
        "granted_secret_refs": [ref.name for ref in worker_request.secret_refs],
        "worker_type": worker_request.worker_type,
        "worker_profile": worker_request.worker_profile,
        "runtime_mode": worker_request.runtime_mode,
    }
    seen: set[str] = set()
    selected_refs: list[dict[str, str]] = []
    candidates = (
        *(
            {"path": str(p), "source": "session_files_touched"}
            for p in session_context["files_touched"]
        ),
        *(
            {"path": str(p), "source": "dependency_files_changed"}
            for d in deps
            for p in d.get("files_changed", [])
        ),
    )
    for r in candidates:
        if r["path"] not in seen:
            seen.add(r["path"])
            selected_refs.append(r)
    if len(selected_refs) > MAX_SELECTED_REFERENCES:
        _add_trunc(tr, "selected_references", len(selected_refs), MAX_SELECTED_REFERENCES)
        selected_refs = selected_refs[:MAX_SELECTED_REFERENCES]
    return session_context, cap_summary, selected_refs


def _assemble_envelope_payload(
    *,
    task_id: str,
    session_id: str | None,
    node_id: str | None,
    dispatch_role: Literal["primary", "decomposed_node"],
    logical_attempt: int,
    task_spec: TaskSpec | None,
    task_text: str,
    memory_context: MemoryContext | None,
    worker_request: WorkerRequest,
    prior_node_context: dict[str, Any] | None,
    workspace_path: Path | None,
    trusted_git_dir: Path | None,
    truncations: list[TruncationRecord],
) -> dict[str, Any]:
    intent, repo_facts, instructions, skills = _project_intent_and_repo(
        task_spec,
        task_text,
        worker_request,
        workspace_path,
        truncations,
        trusted_git_dir,
    )
    deps = _extract_dependencies(prior_node_context, truncations)
    gated_mems = _extract_gated_memories(memory_context, truncations)
    gate_diag = _bound_json_value(
        getattr(memory_context, "gate_diagnostics", {}) or {},
        max_str=500,
        max_list=20,
        max_keys=20,
        field="gate_diagnostics_summary",
        truncations=truncations,
    )
    session_ctx, cap_summary, selected_refs = _project_context_sections(
        memory_context, worker_request, task_spec, deps, truncations
    )
    meta = {
        "schema_version": 1,
        "envelope_id": uuid4().hex,
        "task_id": task_id,
        "session_id": session_id,
        "run_id": None,
        "node_id": node_id,
        "dispatch_role": dispatch_role,
        "logical_attempt": logical_attempt,
        "assembled_at": utc_now(),
        "truncations": [t.model_dump() for t in truncations],
    }
    content = {
        "repo_facts": repo_facts,
        "repo_instructions_snapshot": instructions,
        "repo_skills": skills,
        "dependency_outputs": deps,
        "gated_memory_entries": gated_mems,
        "gate_diagnostics_summary": gate_diag if isinstance(gate_diag, dict) else {},
        "session_context": session_ctx,
        "capability_summary": cap_summary,
        "selected_references": selected_refs,
    }
    return {**meta, **content, **intent}


def assemble_context_envelope(
    *,
    task_id: str,
    session_id: str | None,
    node_id: str | None = None,
    dispatch_role: Literal["primary", "decomposed_node"] = "primary",
    logical_attempt: int = 1,
    task_spec: TaskSpec | None,
    task_text: str,
    memory_context: MemoryContext | None,
    worker_request: WorkerRequest,
    prior_node_context: dict[str, Any] | None = None,
    workspace_path: Path | None = None,
    trusted_git_dir: Path | None = None,
) -> tuple[ContextEnvelope | None, Literal["assembled", "omitted_oversize", "failed"]]:
    """Assemble a bounded, sanitized context envelope prior to worker dispatch."""
    try:
        truncations: list[TruncationRecord] = []
        payload = _assemble_envelope_payload(
            task_id=task_id,
            session_id=session_id,
            node_id=node_id,
            dispatch_role=dispatch_role,
            logical_attempt=logical_attempt,
            task_spec=task_spec,
            task_text=task_text,
            memory_context=memory_context,
            worker_request=worker_request,
            prior_node_context=prior_node_context,
            workspace_path=workspace_path,
            trusted_git_dir=trusted_git_dir,
            truncations=truncations,
        )
        sanitized = _sanitize_dict(payload, list((worker_request.secrets or {}).values()))
        if not _apply_progressive_truncation(sanitized, truncations):
            logger.warning("Context envelope for task %s exceeded 256 KiB ceiling", task_id)
            return None, "omitted_oversize"

        sanitized["truncations"] = [t.model_dump() for t in truncations]
        sanitized["context_content_digest"] = "0" * 64
        sanitized["evidence_digest"] = "0" * 64

        candidate = ContextEnvelope.model_validate(sanitized)
        candidate_json = candidate.model_dump(mode="json")
        cd, ed = _compute_digests(candidate_json)
        final_env = candidate.model_copy(
            update={"context_content_digest": cd, "evidence_digest": ed}
        )

        final_json_bytes = json.dumps(final_env.model_dump(mode="json")).encode("utf-8")
        if len(final_json_bytes) > MAX_ENVELOPE_BYTES:
            logger.warning("Context envelope for task %s exceeded 256 KiB ceiling", task_id)
            return None, "omitted_oversize"

        return final_env, "assembled"
    except Exception:
        logger.warning(
            "Unexpected error assembling context envelope for task %s", task_id, exc_info=True
        )
        return None, "failed"

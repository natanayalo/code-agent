"""Toy sandbox task copied into Codex worker workspaces and executed in-container."""

import json
import sys
from pathlib import Path

repo = Path("/workspace/repo")
context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
report_path = repo / ".code-agent" / "codex-worker-report.md"
report_path.parent.mkdir(parents=True, exist_ok=True)
top_level_entries = sorted(path.name for path in repo.iterdir())

report_lines = [
    "# Codex Worker Report",
    "",
    f"Task: {context['task_text']}",
    f"Session: {context['session_id']}",
    f"Repo URL: {context['repo_url']}",
    f"Branch: {context['branch']}",
    "",
    "Top-level repo entries:",
]
report_lines.extend(f"- {entry}" for entry in top_level_entries[:20] or ["(none)"])
report_lines.extend(
    [
        "",
        "Memory context:",
        json.dumps(context["memory_context"], indent=2, sort_keys=True),
        "",
        "Constraints:",
        json.dumps(context["constraints"], indent=2, sort_keys=True),
        "",
        "Budget:",
        json.dumps(context["budget"], indent=2, sort_keys=True),
    ]
)
report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
print(f"Wrote {report_path.relative_to(repo)}")

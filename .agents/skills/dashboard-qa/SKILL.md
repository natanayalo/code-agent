---
name: dashboard-qa
description: Use when auditing, testing, or polishing the code-agent dashboard UI with browser-based QA evidence. Prefer Codex `browser:control-in-app-browser` when available; otherwise use existing Playwright or equivalent rendered browser automation.
---

# Dashboard QA

Use this skill for repeatable visual and interaction QA of the `dashboard/` React app.

## Browser Surface

Prefer Codex `browser:control-in-app-browser` for rendered dashboard verification when it is available. Read and follow that skill before browser work.

If Codex browser is unavailable, use existing local Playwright, Puppeteer, or equivalent browser automation only when already present. Do not install new browser dependencies for QA.

Do not use Antigravity `/browser` unless the user explicitly opts into controlling the active Chrome instance or provides an isolated disposable browser profile.

Do not count static fetches such as `read_url_content`, raw HTML, API responses, or source inspection as visual QA evidence.

## Workflow

1. Start the dashboard with the repo-standard command (`npm run dev` or `npm run preview`) and record the URL.
2. Audit the core routes: `/`, `/sessions`, `/triggers`, `/proposals`, `/knowledge-base`, `/metrics`, `/system`, and `/settings`.
3. Check at least one desktop viewport and one mobile viewport.
4. Inspect rendered layout and interactions for overflow, cramped controls, missing empty/error/loading states, weak focus/hover states, and obvious regressions.
5. Apply only high-confidence, surgical fixes. Avoid broad redesigns, new dependencies, and unrelated docs churn.
6. Add or update colocated dashboard tests for every touched component.
7. Capture QA evidence with screenshots, visible-page notes, or both.

## Verification

Run these from `dashboard/`:

```bash
npm run lint
npm run test:run
npm run test:coverage
npm run build
```

Validate this skill after edits with:

```bash
.venv/bin/python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/dashboard-qa
```

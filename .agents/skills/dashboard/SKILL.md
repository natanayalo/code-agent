---
name: dashboard
description: Project-specific React/Vite dashboard development, component conventions, and verification.
---

# Dashboard development

Read [AGENTS.md](../../../AGENTS.md), [dashboard README](../../../dashboard/README.md),
and the relevant task before editing. Follow existing component, typography,
color, spacing, and interaction patterns; redesign only when the task calls for it.

- Use TypeScript/React, existing vanilla CSS, @tanstack/react-query for server
  state, and lucide-react icons.
- Keep backend persistence outside dashboard/ and use the existing API service
  and typed contracts.
- Add a colocated *.test.tsx for new components. Verify behavior and accessible
  labels/roles; cover sorting/grouping when changed.
- Operator controls require component, API/service contract, and interaction-state
  smoke coverage under AGENTS.md. Preserve the 90% coverage gate.

Run from dashboard/:

```bash
npm run lint
npm run test:run
npm run test:coverage
```

For rendered layout/interaction checks use the
[dashboard-qa skill](../dashboard-qa/SKILL.md). Keep fixes scoped; do not add
dependencies for routine UI changes.

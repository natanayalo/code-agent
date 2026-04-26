---
name: dashboard
description: Project-specific guidance for React/Vite dashboard development, testing, and aesthetics in this repository.
---

# Dashboard Development Skill

Use this skill when a task involves modifying or adding to the `dashboard/` React application.

## Canonical references

Read these first:
- `AGENTS.md`
- `dashboard/README.md`
- `docs/status.md` (Milestone B)

## Design Aesthetics

Follow the "Design Aesthetics" section in repository instructions:
- Use rich, premium aesthetics (dark mode, glassmorphism, smooth gradients).
- Use modern typography (Inter, Outfit, etc.).
- Add subtle micro-animations and hover effects.
- Avoid generic colors; use curated harmonious palettes.

## Tech Stack & Conventions

- **Logic**: Use TypeScript and React.
- **Styling**: Use Vanilla CSS in `index.css` or component-specific CSS.
- **State**: Use `react-query` for server state.
- **Icons**: Use `lucide-react`.

## Test Conventions

We maintain a **90% coverage threshold**.
- Create a `*.test.tsx` file for every new component.
- Use `vitest` and `react-testing-library`.
- Mock API services using `vi.mock('./services/api')`.
- Verify behavior and accessibility (roles, labels), not just rendering.
- For lists/boards, explicitly verify sorting and grouping logic.

## Verification Pattern

Run these from the `dashboard/` directory:
- `npm run lint`: Check for style and type issues.
- `npm run test:run`: Run all tests once.
- `npm run test:coverage`: Verify that coverage still meets the 90% threshold.

## Scope Guardrails

- Do not add heavy external dependencies without justification.
- Do not bypass the 90% coverage gate.
- Ensure all interactive elements have unique, descriptive IDs or labels for testing.

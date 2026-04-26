# Dashboard Operator UI

The dashboard is a React-based PWA designed for monitoring and controlling the `code-agent` service. It provides a task status board, detailed task views, and will soon support approval workflows and task replay.

## Tech Stack

- **Framework**: [Vite](https://vitejs.dev/) + [React](https://reactjs.org/)
- **Styling**: Vanilla CSS with a custom design system
- **State Management**: [TanStack Query](https://tanstack.com/query/latest) (React Query)
- **Icons**: [Lucide React](https://lucide.dev/)
- **Testing**: [Vitest](https://vitest.dev/) + [React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)

## Development

### Prerequisites

- Node.js 18+
- npm 9+

### Commands

```bash
# Install dependencies
npm install

# Start development server
npm run dev

# Build for production
npm run build

# Run linting
npm run lint

# Run tests
npm run test

# Run tests with coverage (90% threshold)
npm run test:coverage
```

## Testing Policy

All new dashboard components and logic must include tests. We maintain a **90% coverage threshold** for:
- Statements
- Branches
- Functions
- Lines

Tests are located alongside the components in `*.test.tsx` files.

## CI/CD

- **Pre-commit**: Local `dashboard-lint` hook runs on changed dashboard files.
- **GitHub Actions**: Every push or PR involving `dashboard/**` triggers the `Dashboard CI` workflow, which runs linting and the full test suite with coverage enforcement.

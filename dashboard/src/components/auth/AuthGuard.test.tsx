import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AuthGuard } from './AuthGuard';
import { useAuth } from './AuthContext';

vi.mock('./AuthContext', () => ({
  useAuth: vi.fn(),
}));

function renderGuard() {
  return render(
    <MemoryRouter initialEntries={['/protected']}>
      <Routes>
        <Route
          path="/protected"
          element={
            <AuthGuard>
              <div>Protected content</div>
            </AuthGuard>
          }
        />
        <Route path="/login" element={<div>Login route</div>} />
      </Routes>
    </MemoryRouter>
  );
}

describe('AuthGuard', () => {
  it('renders dashboard-owned loading state while checking the session', () => {
    vi.mocked(useAuth).mockReturnValue({
      authenticated: false,
      loading: true,
      login: vi.fn(),
      logout: vi.fn(),
    });

    renderGuard();

    expect(screen.getByRole('status', { name: 'Checking dashboard session' })).toHaveClass(
      'auth-loading-screen'
    );
    expect(document.querySelector('.spinner')).toBeInTheDocument();
  });

  it('redirects unauthenticated users to login', () => {
    vi.mocked(useAuth).mockReturnValue({
      authenticated: false,
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
    });

    renderGuard();

    expect(screen.getByText('Login route')).toBeInTheDocument();
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument();
  });
});

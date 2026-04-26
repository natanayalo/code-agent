import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AuthProvider, useAuth } from './AuthContext';
import { api } from '../../services/api';

vi.mock('../../services/api', () => ({
  api: {
    auth: {
      status: vi.fn(),
      login: vi.fn(),
      logout: vi.fn(),
    },
  },
}));

const TestComponent = () => {
  const { authenticated, loading, login, logout } = useAuth();
  if (loading) return <div>Loading...</div>;
  return (
    <div>
      <div data-testid="auth-status">{authenticated ? 'Authenticated' : 'Not Authenticated'}</div>
      <button onClick={() => login('test-secret')}>Login</button>
      <button onClick={() => logout()}>Logout</button>
    </div>
  );
};

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('provides authentication state and methods', async () => {
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: false });

    await act(async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );
    });

    expect(screen.getByTestId('auth-status')).toHaveTextContent('Not Authenticated');

    vi.mocked(api.auth.login).mockResolvedValue({ status: 'ok' });
    await act(async () => {
      screen.getByText('Login').click();
    });
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Authenticated');
    expect(api.auth.login).toHaveBeenCalledWith('test-secret');

    vi.mocked(api.auth.logout).mockResolvedValue({ status: 'ok' });
    await act(async () => {
      screen.getByText('Logout').click();
    });
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Not Authenticated');
    expect(api.auth.logout).toHaveBeenCalled();
  });

  it('handles status check failure', async () => {
    vi.mocked(api.auth.status).mockRejectedValue(new Error('Network error'));

    await act(async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );
    });

    expect(screen.getByTestId('auth-status')).toHaveTextContent('Not Authenticated');
  });

  it('throws error when useAuth is used outside AuthProvider', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<TestComponent />)).toThrow('useAuth must be used within an AuthProvider');
    consoleSpy.mockRestore();
  });
});

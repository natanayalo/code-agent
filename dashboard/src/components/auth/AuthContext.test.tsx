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
    // Initial status check
    vi.mocked(api.auth.status).mockResolvedValueOnce({ authenticated: false });

    await act(async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );
    });

    expect(screen.getByTestId('auth-status')).toHaveTextContent('Not Authenticated');

    // Login call + verification check
    vi.mocked(api.auth.login).mockResolvedValue({ status: 'ok' });
    vi.mocked(api.auth.status).mockResolvedValueOnce({ authenticated: true });

    await act(async () => {
      screen.getByText('Login').click();
    });

    expect(screen.getByTestId('auth-status')).toHaveTextContent('Authenticated');
    expect(api.auth.login).toHaveBeenCalledWith('test-secret');
    expect(api.auth.status).toHaveBeenCalledTimes(2);

    vi.mocked(api.auth.logout).mockResolvedValue({ status: 'ok' });
    await act(async () => {
      screen.getByText('Logout').click();
    });
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Not Authenticated');
    expect(api.auth.logout).toHaveBeenCalled();
  });

  it('throws error if verification fails after login', async () => {
    vi.mocked(api.auth.status).mockResolvedValueOnce({ authenticated: false });

    // We can test the hook directly using a wrapper or by calling the method from the provider
    let loginFn: (secret: string) => Promise<void> = async () => {};

    const Grabber = () => {
      const { login } = useAuth();
      loginFn = login;
      return null;
    };

    await act(async () => {
      render(
        <AuthProvider>
          <Grabber />
        </AuthProvider>
      );
    });

    vi.mocked(api.auth.login).mockResolvedValue({ status: 'ok' });
    vi.mocked(api.auth.status).mockResolvedValueOnce({ authenticated: false });

    await act(async () => {
      await expect(loginFn('test-secret')).rejects.toThrow('Session could not be established');
    });
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

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { LoginPage } from './LoginPage';
import { useAuth } from './AuthContext';

vi.mock('./AuthContext', () => ({
  useAuth: vi.fn(),
}));

describe('LoginPage', () => {
  const mockLogin = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAuth).mockReturnValue({
      login: mockLogin,
      authenticated: false,
      loading: false,
      logout: vi.fn(),
    });
  });

  it('renders login form', () => {
    render(<LoginPage />);
    expect(screen.getByText('Agent Dashboard')).toBeInTheDocument();
    expect(screen.getByLabelText('Agent Secret')).toBeInTheDocument();
  });

  it('handles successful login', async () => {
    render(<LoginPage />);
    const input = screen.getByLabelText('Agent Secret');
    const button = screen.getByRole('button', { name: /Access Dashboard/i });

    fireEvent.change(input, { target: { value: 'test-secret' } });

    await act(async () => {
      fireEvent.click(button);
    });

    expect(mockLogin).toHaveBeenCalledWith('test-secret');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('handles login failure with error message', async () => {
    mockLogin.mockRejectedValue(new Error('Invalid secret'));
    render(<LoginPage />);
    const input = screen.getByLabelText('Agent Secret');
    const button = screen.getByRole('button', { name: /Access Dashboard/i });

    fireEvent.change(input, { target: { value: 'wrong-secret' } });

    await act(async () => {
      fireEvent.click(button);
    });

    expect(await screen.findByText('Invalid secret')).toBeInTheDocument();
  });

  it('handles generic login failure', async () => {
    mockLogin.mockRejectedValue('Something went wrong');
    render(<LoginPage />);
    const input = screen.getByLabelText('Agent Secret');
    const button = screen.getByRole('button', { name: /Access Dashboard/i });

    fireEvent.change(input, { target: { value: 'secret' } });

    await act(async () => {
      fireEvent.click(button);
    });

    expect(await screen.findByText('Login failed. Please check your secret.')).toBeInTheDocument();
  });

  it('disables button when secret is empty or whitespace', () => {
    const { container } = render(<LoginPage />);
    const input = screen.getByLabelText('Agent Secret');
    const button = screen.getByRole('button', { name: /Access Dashboard/i });

    expect(button).toBeDisabled();

    fireEvent.change(input, { target: { value: '   ' } });
    expect(button).toBeDisabled();

    // Attempting to submit empty secret should not call login
    const form = container.querySelector('form');
    if (form) fireEvent.submit(form);
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('shows loading state during submission', async () => {
    mockLogin.mockReturnValue(new Promise(() => {})); // Never resolves to keep it loading
    render(<LoginPage />);

    const input = screen.getByLabelText('Agent Secret');
    fireEvent.change(input, { target: { value: 'test-secret' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Access Dashboard/i }));
    });

    expect(screen.getByText('Logging in...')).toBeInTheDocument();
    expect(screen.getByRole('button')).toBeDisabled();
  });
});

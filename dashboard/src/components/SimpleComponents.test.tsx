import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { StatsPanel } from './StatsPanel';
import { Header } from './Header';

describe('StatsPanel', () => {
  it('renders stats correctly', () => {
    render(<StatsPanel completed={5} failed={2} />);
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('Tasks Completed')).toBeInTheDocument();
    expect(screen.getByText('Failed Runs')).toBeInTheDocument();
  });
});

describe('Header', () => {
  it('renders header content', () => {
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>
    );
    expect(screen.getByText('Code Agent')).toBeInTheDocument();
    expect(screen.getByText('Operator Dashboard')).toBeInTheDocument();
  });

  it('provides mobile route navigation with the current section active', () => {
    render(
      <MemoryRouter initialEntries={['/metrics']}>
        <Header />
      </MemoryRouter>
    );

    expect(screen.getByRole('navigation', { name: /Dashboard sections/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Metrics/i })).toHaveClass('active');
    expect(screen.getByRole('link', { name: /Tasks/i })).not.toHaveClass('active');
  });

  it('renders authenticated logout action and guards duplicate clicks', async () => {
    let resolveLogout: () => void = () => {};
    const logout = vi.fn(
      () => new Promise<void>((resolve) => {
        resolveLogout = resolve;
      })
    );

    render(
      <MemoryRouter>
        <Header auth={{ authenticated: true, logout }} />
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole('button', { name: /Log out/i }));
    expect(screen.getByRole('button', { name: /Logging out/i })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /Logging out/i }));
    expect(logout).toHaveBeenCalledTimes(1);

    resolveLogout();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Log out/i })).not.toBeDisabled();
    });
  });

  it('recovers when the logout request rejects', async () => {
    const logoutError = new Error('logout failed');
    const logout = vi.fn().mockRejectedValue(logoutError);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    try {
      render(
        <MemoryRouter>
          <Header auth={{ authenticated: true, logout }} />
        </MemoryRouter>
      );

      fireEvent.click(screen.getByRole('button', { name: /Log out/i }));

      await waitFor(() => {
        expect(logout).toHaveBeenCalledTimes(1);
      });
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Log out/i })).not.toBeDisabled();
      });
      expect(warnSpy).toHaveBeenCalledWith(
        'Failed to log out from dashboard header',
        logoutError
      );
    } finally {
      warnSpy.mockRestore();
    }
  });
});

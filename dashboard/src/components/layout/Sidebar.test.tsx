import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { Sidebar } from './Sidebar';

describe('Sidebar', () => {
  it('does not keep Tasks link active on non-root routes', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <Sidebar />
      </MemoryRouter>
    );

    const tasksLink = screen.getByRole('link', { name: /Tasks/i });
    const sessionsLink = screen.getByRole('link', { name: /Sessions/i });

    expect(tasksLink).not.toHaveClass('active');
    expect(sessionsLink).toHaveClass('active');
  });
});

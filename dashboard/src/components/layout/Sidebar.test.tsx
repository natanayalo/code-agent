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
    const triggersLink = screen.getByRole('link', { name: /Triggers/i });
    const knowledgeLink = screen.getByRole('link', { name: /Knowledge Base/i });

    expect(tasksLink).not.toHaveClass('active');
    expect(sessionsLink).toHaveClass('active');
    expect(triggersLink).not.toHaveClass('active');
    expect(knowledgeLink).not.toHaveClass('active');
  });

  it('marks the Triggers link active on the trigger route', () => {
    render(
      <MemoryRouter initialEntries={['/triggers']}>
        <Sidebar />
      </MemoryRouter>
    );

    expect(screen.getByRole('link', { name: /Triggers/i })).toHaveClass('active');
    expect(screen.getByRole('link', { name: /Tasks/i })).not.toHaveClass('active');
  });
});

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
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
    render(<Header />);
    expect(screen.getByText('Code Agent')).toBeInTheDocument();
    expect(screen.getByText('Operator Dashboard')).toBeInTheDocument();
  });
});

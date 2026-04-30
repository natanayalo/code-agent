import { describe, expect, it } from 'vitest';
import { formatLabel } from './formatters';
import { getCategoryThemeClass, getNetworkStyle, getPermissionStyle } from './styleHelpers';

describe('formatLabel', () => {
  it('returns empty string for nullish input', () => {
    expect(formatLabel(undefined)).toBe('');
    expect(formatLabel(null)).toBe('');
    expect(formatLabel('')).toBe('');
  });

  it('formats snake and kebab case labels', () => {
    expect(formatLabel('shell_command')).toBe('Shell Command');
    expect(formatLabel('trace-observability')).toBe('Trace Observability');
  });
});

describe('getPermissionStyle', () => {
  it('returns read_only style', () => {
    expect(getPermissionStyle('read_only')).toEqual({
      color: 'var(--color-text-secondary)',
      showShield: false,
    });
  });

  it('returns warning style for non-read-only permissions', () => {
    expect(getPermissionStyle('dangerous_shell')).toEqual({
      color: 'var(--color-warning)',
      showShield: true,
    });
  });
});

describe('getNetworkStyle', () => {
  it('returns required network style when needed', () => {
    expect(getNetworkStyle(true)).toEqual({
      color: 'var(--color-warning)',
      label: 'Required',
    });
  });

  it('returns none style when network is not required', () => {
    expect(getNetworkStyle(false)).toEqual({
      color: 'var(--color-text-secondary)',
      label: 'None',
    });
  });
});

describe('getCategoryThemeClass', () => {
  it('normalizes category strings into class names', () => {
    expect(getCategoryThemeClass('File IO')).toBe('badge badge-neutral category-file-io');
    expect(getCategoryThemeClass('GitHub/API')).toBe('badge badge-neutral category-github-api');
  });

  it('falls back to default category for nullish values', () => {
    expect(getCategoryThemeClass(null)).toBe('badge badge-neutral category-default');
    expect(getCategoryThemeClass(undefined)).toBe('badge badge-neutral category-default');
  });
});

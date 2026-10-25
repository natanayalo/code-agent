import { describe, expect, it } from 'vitest';
import { formatLabel, isValidHttpUrl } from './formatters';

describe('formatters', () => {
  describe('formatLabel', () => {
    it('formats snake_case and kebab-case strings', () => {
      expect(formatLabel('shell_command')).toBe('Shell Command');
      expect(formatLabel('task-run-status')).toBe('Task Run Status');
    });

    it('preserves uppercase acronyms', () => {
      expect(formatLabel('CI_status')).toBe('CI Status');
      expect(formatLabel('PR_URL')).toBe('PR URL');
    });

    it('returns unknown for nullish or empty input', () => {
      expect(formatLabel(null)).toBe('unknown');
      expect(formatLabel(undefined)).toBe('unknown');
      expect(formatLabel('')).toBe('unknown');
    });
  });

  describe('isValidHttpUrl', () => {
    it('returns true for valid http and https URLs', () => {
      expect(isValidHttpUrl('http://localhost:3000')).toBe(true);
      expect(isValidHttpUrl('https://github.com/octocat/repo')).toBe(true);
    });

    it('returns false for non-http protocols', () => {
      expect(isValidHttpUrl('ftp://files.example.com')).toBe(false);
      expect(isValidHttpUrl('javascript:alert(1)')).toBe(false);
      expect(isValidHttpUrl('mailto:dev@example.com')).toBe(false);
    });

    it('returns false for invalid url strings and nullish values', () => {
      expect(isValidHttpUrl('not a valid url')).toBe(false);
      expect(isValidHttpUrl('')).toBe(false);
      expect(isValidHttpUrl(null)).toBe(false);
      expect(isValidHttpUrl(undefined)).toBe(false);
    });
  });
});

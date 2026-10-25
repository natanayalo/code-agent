/**
 * Converts snake_case or kebab-case strings into capitalized labels.
 * Example: "shell_command" -> "Shell Command"
 */
export const formatLabel = (value: string | undefined | null): string => {
  if (!value) return 'unknown';

  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map(word => {
      if (word.toUpperCase() === word) {
        return word;
      }
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(' ');
};

/**
 * Validates that a string is a safe HTTP or HTTPS URL.
 */
export const isValidHttpUrl = (url: string | null | undefined): boolean => {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
};

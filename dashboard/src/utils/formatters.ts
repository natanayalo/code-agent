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

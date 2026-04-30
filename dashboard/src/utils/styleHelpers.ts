/**
 * Style helpers for mapping API status values to theme variables and labels.
 */

export const getPermissionStyle = (permission: string) => {
  if (permission === 'read_only') {
    return {
      color: 'var(--color-text-secondary)',
      showShield: false,
    };
  }

  return {
    color: 'var(--color-warning)',
    showShield: true,
  };
};

export const getNetworkStyle = (required: boolean) => {
  return {
    color: required ? 'var(--color-warning)' : 'var(--color-text-secondary)',
    label: required ? 'Required' : 'None',
  };
};

/**
 * Maps a tool capability category to a CSS theme class.
 */
export const getCategoryThemeClass = (): string => {
  // For now, all categories use the neutral badge style
  // but this can be extended for specific category coloring.
  return 'badge badge-neutral';
};

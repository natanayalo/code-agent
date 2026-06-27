import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { OperatorInbox } from './OperatorInbox';

describe('OperatorInbox', () => {
  it('renders truncation markup and keeps pending count visible', () => {
    const onOpenTask = vi.fn();
    const longTaskText =
      'This is a very long task text that should truncate in the inbox row without hiding count';

    render(
      <OperatorInbox
        interactions={[
          {
            interaction: {
              interaction_id: 'int-1',
              interaction_type: 'clarification',
              status: 'pending',
              summary: 'Need more details',
              hitl_mode: 'blocking',
              data: {},
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
            task_id: 'task-1',
            task_text: longTaskText,
            status: 'in_progress',
            priority: 1,
          },
        ]}
        selectedTaskId={null}
        onOpenTask={onOpenTask}
      />
    );

    const text = screen.getByTitle(longTaskText);
    expect(text).toHaveClass('operator-inbox-text', 'truncate');
    expect(screen.getByText('clarification')).toHaveClass('operator-inbox-type', 'badge');

    fireEvent.click(screen.getByRole('button', { name: /very long task text/i }));
    expect(onOpenTask).toHaveBeenCalledWith('task-1');
  });
});

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { OperatorInbox } from './OperatorInbox';
import { TaskStatus } from '../types/task';

describe('OperatorInbox', () => {
  it('renders truncation markup and keeps pending count visible', () => {
    const onOpenTask = vi.fn();
    const longTaskText =
      'This is a very long task text that should truncate in the inbox row without hiding count';

    render(
      <OperatorInbox
        tasks={[
          {
            task_id: 'task-1',
            session_id: 'session-1',
            status: TaskStatus.PENDING,
            task_text: longTaskText,
            priority: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            pending_interaction_count: 12,
          },
        ]}
        selectedTaskId={null}
        onOpenTask={onOpenTask}
      />
    );

    const text = screen.getByTitle(longTaskText);
    expect(text).toHaveClass('operator-inbox-text', 'truncate');
    expect(screen.getByText('12 pending')).toHaveClass('operator-inbox-count');

    fireEvent.click(screen.getByRole('button', { name: /very long task text/i }));
    expect(onOpenTask).toHaveBeenCalledWith('task-1');
  });
});

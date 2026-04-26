import React, { useState, useMemo } from 'react';
import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { TaskCard } from './TaskCard';
import { RefreshCw, LayoutGrid, List } from 'lucide-react';

const COLUMNS = [
  {
    id: 'active',
    title: 'Active',
    statuses: [TaskStatus.PENDING, TaskStatus.IN_PROGRESS]
  },
  {
    id: 'completed',
    title: 'Completed',
    statuses: [TaskStatus.COMPLETED]
  },
  {
    id: 'failed',
    title: 'Failed',
    statuses: [TaskStatus.FAILED, TaskStatus.CANCELLED]
  },
];

const STATUS_TO_COLUMN: Partial<Record<TaskStatus, string>> = Object.fromEntries(
  COLUMNS.flatMap(col => col.statuses.map(status => [status, col.id] as const))
);

interface TaskBoardProps {
  tasks: TaskSummarySnapshot[];
  loading: boolean;
  isFetching: boolean;
  error: unknown;
  refetch: () => void;
}

export function TaskBoard({ tasks, loading, isFetching, error, refetch }: TaskBoardProps) {
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const hasError = error != null;
  const isInitialLoading = loading && tasks.length === 0;

  const groupedTasks = useMemo(() => {
    const groups: Record<string, TaskSummarySnapshot[]> = {};
    COLUMNS.forEach(col => {
      groups[col.id] = [];
    });

    tasks.forEach(task => {
      const columnId = STATUS_TO_COLUMN[task.status];
      if (columnId) {
        groups[columnId].push(task);
      }
    });

    Object.values(groups).forEach(group => {
      group.sort((a, b) => {
        const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
        const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
        return bTime - aTime;
      });
    });

    return groups;
  }, [tasks]);

  const errorMessage = error instanceof Error ? error.message : 'Failed to connect to the agent service.';

  return (
    <div className="task-board-container">
      {hasError && (
        <div className="board-error-banner">
          <p>{errorMessage}</p>
          <button onClick={() => refetch()}>Try Again</button>
        </div>
      )}
      <div className="board-header">
        <div className="board-info">
          <h2 className="board-title">Task Status Board</h2>
          <p className="board-subtitle">Real-time view of agent execution pipeline</p>
        </div>
        <div className="board-actions">
          <button
            className="icon-button"
            onClick={() => refetch()}
            disabled={isFetching}
            aria-label="Refresh tasks"
          >
            <RefreshCw size={18} className={isFetching ? 'spin' : ''} />
          </button>
          <div className="view-toggle">
            <button
              className={`toggle-button ${viewMode === 'grid' ? 'active' : ''}`}
              onClick={() => setViewMode('grid')}
              aria-label="Grid view"
            >
              <LayoutGrid size={18} />
            </button>
            <button
              className={`toggle-button ${viewMode === 'list' ? 'active' : ''}`}
              onClick={() => setViewMode('list')}
              aria-label="List view"
            >
              <List size={18} />
            </button>
          </div>
        </div>
      </div>

      <div className={`task-board view-${viewMode}`}>
        {COLUMNS.map(column => {
          const columnTasks = groupedTasks[column.id] || [];
          return (
            <div key={column.id} className="board-column">
              <div className="column-header">
                <h3 className="column-title">{column.title}</h3>
                <span className="column-count">{columnTasks.length}</span>
              </div>
              <div className="column-tasks">
                {isInitialLoading && (
                  <div className="empty-column">Loading tasks...</div>
                )}
                {columnTasks.map(task => (
                  <TaskCard key={task.task_id} task={task} />
                ))}
                {columnTasks.length === 0 && !loading && (
                  <div className="empty-column">No tasks</div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

import React, { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../services/api';
import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { TaskCard } from './TaskCard';
import { RefreshCw, LayoutGrid, List } from 'lucide-react';

const REFRESH_INTERVAL_MS = 5000;

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

export function TaskBoard() {
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');

  const {
    data: tasks = [],
    isLoading: loading,
    error,
    refetch
  } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.listTasks(),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const groupedTasks = useMemo(() => {
    const groups: Record<string, TaskSummarySnapshot[]> = {
      active: [],
      completed: [],
      failed: []
    };

    tasks.forEach(task => {
      const column = COLUMNS.find(col => col.statuses.includes(task.status));
      if (column) {
        groups[column.id].push(task);
      }
    });

    Object.values(groups).forEach(group => {
      group.sort((a, b) => {
        const dateA = a.created_at || '';
        const dateB = b.created_at || '';
        return dateB > dateA ? 1 : dateB < dateA ? -1 : 0;
      });
    });

    return groups;
  }, [tasks]);

  const errorMessage = error instanceof Error ? error.message : 'Failed to connect to the agent service.';

  return (
    <div className="task-board-container">
      {error && (
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
          <button className="icon-button" onClick={() => refetch()} disabled={loading}>
            <RefreshCw size={18} className={loading ? 'spin' : ''} />
          </button>
          <div className="view-toggle">
            <button
              className={`toggle-button ${viewMode === 'grid' ? 'active' : ''}`}
              onClick={() => setViewMode('grid')}
            >
              <LayoutGrid size={18} />
            </button>
            <button
              className={`toggle-button ${viewMode === 'list' ? 'active' : ''}`}
              onClick={() => setViewMode('list')}
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

import React, { useEffect, useState, useMemo, useRef, useCallback } from 'react';
import { api } from '../services/api';
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
  }
];

const REFRESH_INTERVAL_MS = 30000;

export function TaskBoard() {
  const [tasks, setTasks] = useState<TaskSummarySnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const isPollingRef = useRef(false);
  const isMountedRef = useRef(true);

  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');

  const loadTasks = useCallback(async (isAutoRefresh = false) => {
    if (!isAutoRefresh && isMountedRef.current) {
      setLoading(true);
    }

    if (isPollingRef.current || !isMountedRef.current) return;

    isPollingRef.current = true;
    try {
      const data = await api.listTasks();
      if (isMountedRef.current) {
        setTasks(data);
        setError(null);
      }
    } catch (err) {
      console.error('Failed to load tasks:', err);
      if (isMountedRef.current && !isAutoRefresh) {
        setError('Failed to connect to the agent service. Please check your connection.');
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
        isPollingRef.current = false;

        // Schedule next poll only after current one finishes
        if (pollTimerRef.current) {
          clearTimeout(pollTimerRef.current);
        }
        pollTimerRef.current = setTimeout(() => loadTasks(true), REFRESH_INTERVAL_MS);
      }
    }
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    loadTasks();
    return () => {
      isMountedRef.current = false;
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
      }
    };
  }, [loadTasks]);

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

  return (
    <div className="task-board-container">
      {error && (
        <div className="board-error-banner">
          <p>{error}</p>
          <button onClick={() => loadTasks()}>Try Again</button>
        </div>
      )}
      <div className="board-header">
        <div className="board-info">
          <h2 className="board-title">Task Status Board</h2>
          <p className="board-subtitle">Real-time view of agent execution pipeline</p>
        </div>
        <div className="board-actions">
          <button className="icon-button" onClick={() => loadTasks()} disabled={loading}>
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

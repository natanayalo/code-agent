import React, { useEffect, useState } from 'react';
import { api } from '../services/api';
import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { TaskCard } from './TaskCard';
import { RefreshCw, LayoutGrid, List } from 'lucide-react';

export function TaskBoard() {
  const [tasks, setTasks] = useState<TaskSummarySnapshot[]>([]);
  const [loading, setLoading] = useState(true);

  const loadTasks = async () => {
    setLoading(true);
    try {
      const data = await api.listTasks();
      setTasks(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTasks();
    const interval = setInterval(loadTasks, 30000); // Refresh every 30s
    return () => clearInterval(interval);
  }, []);

  const columns = [
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

  return (
    <div className="task-board-container">
      <div className="board-header">
        <div className="board-info">
          <h2 className="board-title">Task Status Board</h2>
          <p className="board-subtitle">Real-time view of agent execution pipeline</p>
        </div>
        <div className="board-actions">
          <button className="icon-button" onClick={loadTasks} disabled={loading}>
            <RefreshCw size={18} className={loading ? 'spin' : ''} />
          </button>
          <div className="view-toggle">
            <button className="toggle-button active"><LayoutGrid size={18} /></button>
            <button className="toggle-button"><List size={18} /></button>
          </div>
        </div>
      </div>

      <div className="task-board">
        {columns.map(column => (
          <div key={column.id} className="board-column">
            <div className="column-header">
              <h3 className="column-title">{column.title}</h3>
              <span className="column-count">
                {tasks.filter(t => column.statuses.includes(t.status)).length}
              </span>
            </div>
            <div className="column-tasks">
              {tasks
                .filter(t => column.statuses.includes(t.status))
                .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
                .map(task => (
                  <TaskCard key={task.task_id} task={task} />
                ))}
              {tasks.filter(t => column.statuses.includes(t.status)).length === 0 && (
                <div className="empty-column">No tasks</div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

import React, { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { DashboardLayout } from './components/layout/DashboardLayout';
import { TaskBoard } from './components/TaskBoard';
import { StatsPanel } from './components/StatsPanel';
import { api } from './services/api';
import { TaskStatus } from './types/task';

const REFRESH_INTERVAL_MS = 30000;

function App() {
  const {
    data: tasks = [],
    isLoading: loading,
    isFetching,
    error,
    refetch
  } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.listTasks(),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const stats = useMemo(() => {
    return tasks.reduce(
      (acc, t) => {
        if (t.status === TaskStatus.COMPLETED) {
          acc.completed++;
        } else if (t.status === TaskStatus.FAILED || t.status === TaskStatus.CANCELLED) {
          acc.failed++;
        }
        return acc;
      },
      { completed: 0, failed: 0 }
    );
  }, [tasks]);

  return (
    <DashboardLayout>
      <div className="dashboard-summary">
        <StatsPanel
          completed={stats.completed}
          failed={stats.failed}
        />
      </div>

      <TaskBoard
        tasks={tasks}
        loading={loading}
        isFetching={isFetching}
        error={error}
        refetch={refetch}
      />
    </DashboardLayout>
  );
}

export default App;

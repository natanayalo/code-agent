import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { DashboardLayout } from './components/layout/DashboardLayout';
import { TaskBoard } from './components/TaskBoard';
import { StatsPanel } from './components/StatsPanel';
import { api } from './services/api';
import { TaskStatus } from './types/task';
import { AuthProvider, useAuth } from './components/auth/AuthContext';
import { AuthGuard } from './components/auth/AuthGuard';
import { LoginPage } from './components/auth/LoginPage';
import { SessionsPage } from './components/SessionsPage';
import { MetricsPage } from './components/MetricsPage';
import { TaskDetailPanel } from './components/TaskDetailPanel';
import { OperatorInbox } from './components/OperatorInbox';

const REFRESH_INTERVAL_MS = 30000;

function DashboardContent() {
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
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

  const {
    data: selectedTask = null,
    isLoading: taskDetailLoading,
    error: taskDetailError,
    refetch: refetchTaskDetail,
  } = useQuery({
    queryKey: ['task-detail', selectedTaskId],
    queryFn: () => api.getTask(selectedTaskId as string),
    enabled: selectedTaskId !== null,
    refetchInterval: selectedTaskId ? REFRESH_INTERVAL_MS : false,
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

  const inboxTasks = useMemo(
    () => tasks.filter((task) => (task.pending_interaction_count || 0) > 0),
    [tasks]
  );

  return (
    <DashboardLayout>
      <div className="dashboard-summary">
        <StatsPanel
          completed={stats.completed}
          failed={stats.failed}
        />
      </div>

      <OperatorInbox
        tasks={inboxTasks}
        selectedTaskId={selectedTaskId}
        onOpenTask={setSelectedTaskId}
      />

      <main className={`dashboard-content ${selectedTaskId ? 'dashboard-content-with-panel' : ''}`}>
        <div className="dashboard-content-inner">
          <TaskBoard
            tasks={tasks}
            loading={loading}
            isFetching={isFetching}
            error={error}
            refetch={refetch}
            selectedTaskId={selectedTaskId}
            onTaskSelect={setSelectedTaskId}
          />

          <TaskDetailPanel
            task={selectedTask}
            loading={taskDetailLoading}
            error={taskDetailError}
            onClose={() => setSelectedTaskId(null)}
            onRefresh={() => {
              refetch();
              refetchTaskDetail();
            }}
          />
        </div>
      </main>
    </DashboardLayout>
  );
}

function SettingsPage() {
  return (
    <DashboardLayout>
      <div className="empty-state">
        <h3>Settings coming soon</h3>
        <p>Configuration controls are not available yet.</p>
      </div>
    </DashboardLayout>
  );
}

interface LocationState {
  from?: {
    pathname: string;
  };
}

function AppRoutes() {
  const { authenticated } = useAuth();
  const location = useLocation();

  return (
    <Routes>
      <Route
        path="/login"
        element={
          authenticated ? (
            <Navigate to={(location.state as LocationState)?.from?.pathname || '/'} replace />
          ) : (
            <LoginPage />
          )
        }
      />
      <Route
        path="/"
        element={
          <AuthGuard>
            <DashboardContent />
          </AuthGuard>
        }
      />
      <Route
        path="/sessions"
        element={
          <AuthGuard>
            <SessionsPage />
          </AuthGuard>
        }
      />
      <Route
        path="/metrics"
        element={
          <AuthGuard>
            <MetricsPage />
          </AuthGuard>
        }
      />
      <Route
        path="/settings"
        element={
          <AuthGuard>
            <SettingsPage />
          </AuthGuard>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;

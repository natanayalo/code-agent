import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../services/api';
import { SessionSnapshot } from '../types/session';
import { DashboardLayout } from './layout/DashboardLayout';
import { Clock, MessageSquare, User, Activity } from 'lucide-react';


const SESSIONS_REFETCH_INTERVAL_MS = 30000;

function getStatusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === 'active') return 'running';
  if (s === 'closed' || s === 'completed') return 'success';
  if (s === 'failed') return 'error';
  return s;
}

export function SessionsPage() {
  const {
    data: sessions = [],
    isLoading,
    error,
    refetch
  } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.listSessions(),
    refetchInterval: SESSIONS_REFETCH_INTERVAL_MS,
  });

  if (error) {
    return (
      <DashboardLayout>
        <div className="error-container">
          <h2>Error loading sessions</h2>
          <p>{(error as Error).message}</p>
          <button onClick={() => refetch()} className="btn-primary">Retry</button>
        </div>
      </DashboardLayout>
    );
  }

  return (
    <DashboardLayout>
      <div className="page-header">
        <h1>Sessions</h1>
        <p className="page-subtitle">Active and historical conversation threads</p>
      </div>

      {isLoading ? (
        <div className="loading-container">
          <div className="spinner"></div>
          <p>Loading sessions...</p>
        </div>
      ) : sessions.length === 0 ? (
        <div className="empty-state">
          <MessageSquare size={48} />
          <h3>No sessions found</h3>
          <p>New sessions will appear here once tasks are submitted.</p>
        </div>
      ) : (
        <div className="sessions-grid">
          {sessions.map((session: SessionSnapshot) => (
            <div key={session.session_id} className="session-card card">
              <div className="session-card-header">
                <div className={`status-badge status-${getStatusClass(session.status)}`}>
                  {session.status}
                </div>
                <span className="session-id">ID: <span className="truncate" title={session.session_id}>{session.session_id}</span></span>
              </div>

              <div className="session-card-body">
                <div className="session-info-item">
                  <User size={16} />
                  <span>User: </span><span className="truncate">{session.user_id}</span>
                </div>
                <div className="session-info-item">
                  <Activity size={16} />
                  <span>Channel: </span><span>{session.channel}</span>
                </div>
                <div className="session-info-item">
                  <MessageSquare size={16} />
                  <span>Thread: </span><span className="truncate">{session.external_thread_id}</span>
                </div>
                {session.active_task_id && (
                  <div className="session-info-item active-task">
                    <Clock size={16} />
                    <span>Active Task: </span><span className="truncate" title={session.active_task_id || ''}>{session.active_task_id}</span>
                  </div>
                )}
              </div>

              <div className="session-card-footer">
                <span className="timestamp">
                  Created: {new Date(session.created_at).toLocaleString()}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </DashboardLayout>
  );
}

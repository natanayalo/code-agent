import React from 'react';
import { Layout, Play, CheckCircle, Clock, AlertCircle } from 'lucide-react';

function App() {
  return (
    <div style={{ padding: 'var(--spacing-xl)', maxWidth: '1200px', margin: '0 auto', width: '100%' }}>
      <header style={{ marginBottom: 'var(--spacing-xl)', display: 'flex', alignItems: 'center', gap: 'var(--spacing-md)' }}>
        <div style={{
          background: 'var(--color-accent-gradient)',
          padding: 'var(--spacing-sm)',
          borderRadius: '12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center'
        }}>
          <Layout size={24} color="white" />
        </div>
        <div>
          <h1 className="gradient-text" style={{ fontSize: '1.5rem', fontWeight: 700 }}>Code Agent</h1>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>Operator Dashboard</p>
        </div>
      </header>

      <main style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 'var(--spacing-lg)' }}>
        {/* Placeholder Task Card */}
        <div className="glass-panel" style={{ padding: 'var(--spacing-lg)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 'var(--spacing-md)' }}>
            <span style={{
              fontSize: '0.75rem',
              textTransform: 'uppercase',
              letterSpacing: '0.05em',
              background: 'rgba(56, 189, 248, 0.1)', // TODO: Add as variable if used often
              color: 'var(--color-accent-primary)',
              padding: 'var(--spacing-xs) var(--spacing-sm)',
              borderRadius: '4px'
            }}>Running</span>
            <Clock size={16} color="var(--color-text-muted)" />
          </div>
          <h3 style={{ marginBottom: '0.5rem' }}>Implement PWA Frontend Architecture</h3>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem', marginBottom: '1.5rem' }}>
            Designing the core structure and selecting technology stack for the operator dashboard.
          </p>
          <div style={{ borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: 'var(--spacing-md)', display: 'flex', gap: 'var(--spacing-md)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
              <Play size={14} />
              <span>3 commands run</span>
            </div>
          </div>
        </div>

        {/* Stats placeholder */}
        <div className="glass-panel" style={{ padding: 'var(--spacing-lg)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--spacing-md)' }}>
            <div style={{ color: 'var(--color-success)' }}><CheckCircle size={20} /></div>
            <div>
              <div style={{ fontSize: '1.25rem', fontWeight: 600 }}>124</div>
              <div style={{ fontSize: '0.75rem', color: 'var(--color-text-secondary)' }}>Tasks Completed</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--spacing-md)' }}>
            <div style={{ color: 'var(--color-error)' }}><AlertCircle size={20} /></div>
            <div>
              <div style={{ fontSize: '1.25rem', fontWeight: 600 }}>2</div>
              <div style={{ fontSize: '0.75rem', color: 'var(--color-text-secondary)' }}>Failed Runs</div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;

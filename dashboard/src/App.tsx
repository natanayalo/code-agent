import React from 'react';
import { Layout, Play, CheckCircle, Clock, AlertCircle } from 'lucide-react';

function App() {
  return (
    <div style={{ padding: '2rem', maxWidth: '1200px', margin: '0 auto', width: '100%' }}>
      <header style={{ marginBottom: '3rem', display: 'flex', alignItems: 'center', gap: '1rem' }}>
        <div style={{
          background: 'var(--color-accent-gradient)',
          padding: '0.75rem',
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

      <main style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '1.5rem' }}>
        {/* Placeholder Task Card */}
        <div className="glass-panel" style={{ padding: '1.5rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
            <span style={{
              fontSize: '0.75rem',
              textTransform: 'uppercase',
              letterSpacing: '0.05em',
              background: 'rgba(56, 189, 248, 0.1)',
              color: 'var(--color-accent-primary)',
              padding: '0.25rem 0.5rem',
              borderRadius: '4px'
            }}>Running</span>
            <Clock size={16} color="var(--color-text-muted)" />
          </div>
          <h3 style={{ marginBottom: '0.5rem' }}>Implement PWA Frontend Architecture</h3>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem', marginBottom: '1.5rem' }}>
            Designing the core structure and selecting technology stack for the operator dashboard.
          </p>
          <div style={{ borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: '1rem', display: 'flex', gap: '1rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
              <Play size={14} />
              <span>3 commands run</span>
            </div>
          </div>
        </div>

        {/* Stats placeholder */}
        <div className="glass-panel" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            <div style={{ color: 'var(--color-success)' }}><CheckCircle size={20} /></div>
            <div>
              <div style={{ fontSize: '1.25rem', fontWeight: 600 }}>124</div>
              <div style={{ fontSize: '0.75rem', color: 'var(--color-text-secondary)' }}>Tasks Completed</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
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

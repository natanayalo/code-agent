import React, { useState } from 'react';
import { useAuth } from './AuthContext';
import { Lock, ShieldAlert, Loader2 } from 'lucide-react';

export const LoginPage: React.FC = () => {
  const [secret, setSecret] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { login } = useAuth();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!secret.trim()) return;

    setError(null);
    setIsSubmitting(true);

    try {
      await login(secret);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed. Please check your secret.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="login-container">
      <style>{`
        .login-container {
          display: flex;
          align-items: center;
          justify-content: center;
          min-height: 100vh;
          background: var(--bg-main, #0f172a);
          padding: 1.5rem;
        }

        .login-card {
          width: 100%;
          max-width: 400px;
          background: var(--bg-card, #1e293b);
          border: 1px solid var(--border-color, #334155);
          border-radius: 1rem;
          padding: 2.5rem;
          box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
        }

        .login-header {
          text-align: center;
          margin-bottom: 2rem;
        }

        .login-icon {
          width: 3rem;
          height: 3rem;
          color: var(--primary, #3b82f6);
          margin-bottom: 1rem;
        }

        .login-title {
          font-size: 1.5rem;
          font-weight: 700;
          color: var(--text-main, #f8fafc);
          margin-bottom: 0.5rem;
        }

        .login-subtitle {
          color: var(--text-muted, #94a3b8);
          font-size: 0.875rem;
        }

        .login-form {
          display: flex;
          flex-direction: column;
          gap: 1.5rem;
        }

        .form-group {
          display: flex;
          flex-direction: column;
          gap: 0.5rem;
        }

        .form-label {
          font-size: 0.875rem;
          font-weight: 500;
          color: var(--text-main, #f8fafc);
        }

        .form-input {
          background: var(--bg-main, #0f172a);
          border: 1px solid var(--border-color, #334155);
          border-radius: 0.5rem;
          padding: 0.75rem 1rem;
          color: var(--text-main, #f8fafc);
          font-family: inherit;
          font-size: 1rem;
          transition: border-color 0.2s, box-shadow 0.2s;
        }

        .form-input:focus {
          outline: none;
          border-color: var(--primary, #3b82f6);
          box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
        }

        .submit-button {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 0.5rem;
          background: var(--primary, #3b82f6);
          color: white;
          border: none;
          border-radius: 0.5rem;
          padding: 0.75rem;
          font-weight: 600;
          cursor: pointer;
          transition: background 0.2s;
        }

        .submit-button:hover:not(:disabled) {
          background: var(--primary-hover, #2563eb);
        }

        .submit-button:disabled {
          opacity: 0.7;
          cursor: not-allowed;
        }

        .error-message {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          background: rgba(239, 68, 68, 0.1);
          border: 1px solid rgba(239, 68, 68, 0.2);
          color: #ef4444;
          padding: 0.75rem;
          border-radius: 0.5rem;
          font-size: 0.875rem;
        }

        .animate-spin {
          animation: spin 1s linear infinite;
        }

        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>

      <div className="login-card">
        <div className="login-header">
          <Lock className="login-icon mx-auto" />
          <h1 className="login-title">Agent Dashboard</h1>
          <p className="login-subtitle">Enter your secret to access the operator console</p>
        </div>

        <form onSubmit={handleSubmit} className="login-form">
          <div className="form-group">
            <label className="form-label" htmlFor="secret">Agent Secret</label>
            <input
              id="secret"
              type="password"
              className="form-input"
              placeholder="••••••••••••••••"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              required
              disabled={isSubmitting}
              autoFocus
            />
          </div>

          {error && (
            <div className="error-message">
              <ShieldAlert size={18} />
              <span>{error}</span>
            </div>
          )}

          <button
            type="submit"
            className="submit-button"
            disabled={isSubmitting || !secret.trim()}
          >
            {isSubmitting ? (
              <>
                <Loader2 size={18} className="animate-spin" />
                <span>Logging in...</span>
              </>
            ) : (
              <span>Access Dashboard</span>
            )}
          </button>
        </form>
      </div>
    </div>
  );
};

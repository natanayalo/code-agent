import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { api } from '../../services/api';

interface AuthContextType {
  authenticated: boolean;
  loading: boolean;
  login: (secret: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [authenticated, setAuthenticated] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);

  const checkStatus = useCallback(async () => {
    try {
      const { authenticated } = await api.auth.status();
      setAuthenticated(authenticated);
    } catch (error) {
      setAuthenticated(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  const login = async (secret: string) => {
    await api.auth.login(secret);
    // Verify that the session cookie was actually stored and accepted
    const { authenticated: verified } = await api.auth.status();
    setAuthenticated(verified);

    if (!verified) {
      throw new Error(
        'Session could not be established. Please ensure your browser accepts cookies ' +
          'and you are using a secure connection if required.'
      );
    }
  };

  const logout = async () => {
    try {
      await api.auth.logout();
    } finally {
      setAuthenticated(false);
    }
  };

  return (
    <AuthContext.Provider value={{ authenticated, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

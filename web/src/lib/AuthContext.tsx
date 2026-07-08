import { createContext, useContext, useState, type ReactNode } from "react";
import { api, clearToken, getToken, setToken } from "./api";

interface AuthState {
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(() => getToken() !== null);

  async function login(email: string, password: string) {
    const { access_token } = await api.login(email, password);
    setToken(access_token);
    setIsAuthenticated(true);
  }

  async function signup(email: string, password: string) {
    const { access_token } = await api.signup(email, password);
    setToken(access_token);
    setIsAuthenticated(true);
  }

  function logout() {
    clearToken();
    setIsAuthenticated(false);
  }

  return (
    <AuthContext.Provider value={{ isAuthenticated, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

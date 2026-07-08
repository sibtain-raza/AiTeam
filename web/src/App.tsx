import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/AuthContext";
import { AuthPage } from "./pages/AuthPage";
import { RunListPage } from "./pages/RunListPage";
import { RunFloorPage } from "./pages/RunFloorPage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function RedirectIfAuthed({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (isAuthenticated) return <Navigate to="/runs" replace />;
  return <>{children}</>;
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route
            path="/login"
            element={
              <RedirectIfAuthed>
                <AuthPage />
              </RedirectIfAuthed>
            }
          />
          <Route
            path="/runs"
            element={
              <RequireAuth>
                <RunListPage />
              </RequireAuth>
            }
          />
          <Route
            path="/runs/:runId"
            element={
              <RequireAuth>
                <RunFloorPage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;

import { Button, Result, Spin } from "antd";
import { lazy, Suspense, type ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { useAuth } from "./auth/AuthProvider";
import { AppLayout } from "./components/AppLayout";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { NewCheckPage } from "./pages/NewCheckPage";
import { NotFoundPage } from "./pages/NotFoundPage";

// тяжёлые экраны (просмотрщик PDF, редактор правил) — отдельными частями, по требованию
const CheckPage = lazy(() => import("./pages/CheckPage").then((m) => ({ default: m.CheckPage })));
const ComparePage = lazy(() => import("./pages/ComparePage").then((m) => ({ default: m.ComparePage })));
const HistoryPage = lazy(() => import("./pages/HistoryPage").then((m) => ({ default: m.HistoryPage })));
const RulesPage = lazy(() => import("./pages/RulesPage").then((m) => ({ default: m.RulesPage })));

const fallback = (
  <div style={{ display: "grid", placeItems: "center", padding: 64 }}>
    <Spin />
  </div>
);

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100vh" }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

function RequireMethodologist({ children }: { children: ReactNode }) {
  const { can } = useAuth();
  if (can("methodologist")) return <>{children}</>;
  // не молчаливый переход на главную: человек должен понять, почему страница не открылась
  return (
    <Result
      status="403"
      title="Правила редактирует методолог"
      subTitle="У вашей роли нет доступа к редактору правил. Список правил с исходами есть в каждом отчёте — вкладка «Что проверено»."
      extra={
        <Button type="primary">
          <Link to="/new">К новой проверке</Link>
        </Button>
      }
    />
  );
}

export function App() {
  return (
    <Suspense fallback={fallback}>
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route index element={<HomePage />} />
        <Route path="checks" element={<HistoryPage />} />
        <Route path="new" element={<NewCheckPage />} />
        {/* старые адреса: список проверок — /checks, страницы «Качество» нет (метрики — в README) */}
        <Route path="history" element={<Navigate to="/checks" replace />} />
        <Route path="quality" element={<Navigate to="/" replace />} />
        <Route path="checks/:id" element={<CheckPage />} />
        <Route path="checks/:id/compare/:other" element={<ComparePage />} />
        <Route
          path="rules"
          element={
            <RequireMethodologist>
              <RulesPage />
            </RequireMethodologist>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
    </Suspense>
  );
}

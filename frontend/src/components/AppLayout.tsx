import { HomeOutlined, LogoutOutlined } from "@ant-design/icons";
import { Button, Layout, Spin } from "antd";
import { Suspense } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { brand } from "../theme";
import { ErrorBoundary } from "./ErrorBoundary";
import { Logo } from "./Logo";

/**
 * Шапка — знак, кто вошёл и «Выйти» прямо на фоне страницы, без тёмной плашки; под ней на всех
 * страницах, кроме главной, — кнопка «На главную» в том же стиле. Разделов мало, поэтому они —
 * крупные плитки на главной, а не полоса вкладок (решения пользователя, 29.10).
 */
export function AppLayout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const atHome = location.pathname === "/";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header
        className="app-header"
        style={{ display: "flex", alignItems: "center", position: "sticky", top: 0, zIndex: 20 }}
      >
        <Link to="/" className="app-home" aria-label="Светофор — на главную">
          <Logo />
        </Link>
        <span style={{ flex: 1 }} />
        {user && (
          <>
            <span className="header-user-name">{user.full_name || user.email}</span>
            <Button
              className="header-button"
              icon={<LogoutOutlined aria-hidden />}
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Выйти
            </Button>
          </>
        )}
      </Layout.Header>
      <Layout.Content>
        {!atHome && (
          <nav className="app-subbar" aria-label="Навигация">
            {/* ссылка в виде кнопки: средний щелчок и Ctrl+щелчок открывают новую вкладку */}
            <Button
              className="header-button"
              href="/"
              icon={<HomeOutlined aria-hidden />}
              onClick={(event) => {
                if (event.ctrlKey || event.metaKey || event.shiftKey) return;
                event.preventDefault();
                navigate("/");
              }}
            >
              На главную
            </Button>
          </nav>
        )}
        <ErrorBoundary key={location.pathname}>
          <Suspense fallback={<div style={{ display: "grid", placeItems: "center", padding: 64 }}><Spin /></div>}>
            <Outlet />
          </Suspense>
        </ErrorBoundary>
      </Layout.Content>
      <Layout.Footer
        style={{ textAlign: "center", color: brand.textTertiary, fontSize: 12, background: "transparent" }}
      >
        Светофор — подсказка для специалиста, а не юридическое заключение
      </Layout.Footer>
    </Layout>
  );
}

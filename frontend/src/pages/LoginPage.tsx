import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { Logo } from "../components/Logo";
import { safeReturnPath } from "../lib/returnPath";
import { brand } from "../theme";

const DEMO = [
  { email: "specialist@demo.ru", title: "Специалист по закупкам" },
  { email: "methodologist@demo.ru", title: "Методолог (правила)" },
];

type DemoAccess = { enabled: boolean; default_password: boolean };

export function LoginPage() {
  const { user, login } = useAuth();
  // демо-учётки показываются, только если они включены на сервере; пароль «demo» подставляется,
  // только если он и стоит (иначе пароль выдаёт администратор стенда)
  const { data: demo } = useQuery({
    queryKey: ["auth", "demo"],
    queryFn: () => api<DemoAccess>("/api/auth/demo"),
    staleTime: Infinity,
  });
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm<{ email: string; password: string }>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const from = safeReturnPath((location.state as { from?: unknown } | null)?.from);

  if (user) return <Navigate to={from} replace />;

  const submit = async ({ email, password }: { email: string; password: string }) => {
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Не удалось войти");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: 16,
        background: `linear-gradient(180deg, ${brand.navy} 0, ${brand.navy} 38%, ${brand.page} 38%)`,
      }}
    >
      <div style={{ width: "100%", maxWidth: 400 }}>
        <div style={{ marginBottom: 24 }}>
          <Logo inverted size={40} />
        </div>
        <Card styles={{ body: { padding: 28 } }}>
          <Typography.Title level={3} style={{ marginTop: 0, marginBottom: 20 }}>
            Вход
          </Typography.Title>
          {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
          <Form form={form} layout="vertical" requiredMark={false} onFinish={submit}>
            <Form.Item
              name="email"
              label="Электронная почта"
              rules={[{ required: true, message: "Введите электронную почту" }]}
            >
              <Input autoComplete="username" autoFocus size="large" />
            </Form.Item>
            <Form.Item
              name="password"
              label="Пароль"
              rules={[{ required: true, message: "Введите пароль" }]}
            >
              <Input.Password autoComplete="current-password" size="large" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large" loading={busy}>
              Войти
            </Button>
          </Form>
          {demo?.enabled && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: `1px solid ${brand.border}` }}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {demo.default_password
                  ? "Демо-доступ (пароль «demo» — только для демонстрации):"
                  : "Демо-доступ (пароль выдаёт администратор стенда):"}
              </Typography.Text>
              <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
                {DEMO.map((d) => (
                  <button
                    key={d.email}
                    type="button"
                    className="demo-account"
                    onClick={() =>
                      form.setFieldsValue(
                        demo.default_password ? { email: d.email, password: "demo" } : { email: d.email },
                      )
                    }
                  >
                    <span style={{ fontWeight: 500 }}>{d.title}</span>
                    <span className="muted" style={{ fontSize: 13, overflowWrap: "anywhere" }}>
                      {d.email}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

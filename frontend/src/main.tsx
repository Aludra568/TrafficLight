import "@fontsource/onest/400.css";
import "@fontsource/onest/500.css";
import "@fontsource/onest/600.css";
import "@fontsource/onest/700.css";
import "./index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
// ES-сборка локали: «antd/locale/ru_RU» в Vite приходит как CommonJS с двойным default,
// и Ant Design молча остаётся на английском («(optional)», «No data», OK/Cancel)
import ruRU from "antd/es/locale/ru_RU";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./api/client";
import { AuthProvider } from "./auth/AuthProvider";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { theme } from "./theme";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // ошибки доступа и «не найдено» не повторяем — ответ не изменится
      retry: (attempt, error) =>
        !(error instanceof ApiError && [401, 403, 404, 409].includes(error.status)) && attempt < 2,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={ruRU} theme={theme}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AuthProvider>
              <ErrorBoundary>
                <App />
              </ErrorBoundary>
            </AuthProvider>
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
// ES-сборка локали: «antd/locale/ru_RU» в Vite приходит как CommonJS с двойным default,
// и Ant Design молча остаётся на английском («(optional)», «No data», OK/Cancel)
import ruRU from "antd/es/locale/ru_RU";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { AuthProvider } from "../auth/AuthProvider";

/** Ответы «сервера» для тестов: путь (с методом или без) → тело ответа или функция. */
export type Routes = Record<string, unknown | ((init?: RequestInit) => unknown)>;

export function mockApi(routes: Routes) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const path = url.split("?")[0];
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push(`${method} ${url}`);
    const handler = routes[`${method} ${path}`] ?? routes[path];
    if (handler === undefined) {
      return new Response(JSON.stringify({ error: { code: "not_found", message: "Нет" } }), {
        status: 404,
      });
    }
    const body = typeof handler === "function" ? (handler as (i?: RequestInit) => unknown)(init) : handler;
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

export function renderApp(ui: ReactNode, path = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <ConfigProvider locale={ruRU}>
      <AntApp>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={[path]}>
            <AuthProvider>{ui}</AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}

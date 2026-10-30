import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { tokenStore } from "./api/client";
import { mockApi, renderApp } from "./test/render";

const SPECIALIST = {
  id: "u1",
  email: "specialist@demo.ru",
  full_name: "Специалист по закупкам (демо)",
  role: "specialist",
};

const EMPTY_HOME = {
  "/api/rulesets": [{ code: "44fz-supplier-base", title: "44-ФЗ: риски поставщика", version: 1 }],
  "/api/settings": { limits: { max_upload_mb: 100 } },
  "/api/checks": { items: [], total: 0, page: 1, page_size: 5 },
};

describe("вход", () => {
  beforeEach(() => tokenStore.set(null));
  afterEach(() => {
    tokenStore.set(null);
    vi.unstubAllGlobals();
  });

  it("после входа открывает главную, а не остаётся на странице входа", async () => {
    mockApi({
      "POST /api/auth/login": { access_token: "t1", token_type: "bearer", user: SPECIALIST },
      "/api/auth/me": SPECIALIST,
      ...EMPTY_HOME,
    });
    render(renderApp(<App />, "/login"));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Электронная почта"), "specialist@demo.ru");
    await user.type(screen.getByLabelText("Пароль"), "demo");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("heading", { name: "Главная" })).toBeInTheDocument();
    expect(tokenStore.get()).toBe("t1");
  });

  it("демо-учётки на странице входа — только если включены на сервере", async () => {
    mockApi({ "/api/auth/demo": { enabled: false, default_password: false } });
    render(renderApp(<App />, "/login"));
    expect(await screen.findByRole("heading", { name: "Вход" })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/Демо-доступ/)).toBeNull();
  });

  it("демо-учётки с паролем по умолчанию — с предупреждением", async () => {
    mockApi({ "/api/auth/demo": { enabled: true, default_password: true } });
    render(renderApp(<App />, "/login"));
    expect(await screen.findByText(/пароль «demo» — только для демонстрации/)).toBeInTheDocument();
  });

  it("без входа отправляет на страницу входа", async () => {
    mockApi(EMPTY_HOME);
    render(renderApp(<App />, "/history"));
    expect(await screen.findByRole("heading", { name: "Вход" })).toBeInTheDocument();
  });

  it("неверный пароль — сообщение сервера по-русски", async () => {
    mockApi({
      "POST /api/auth/login": () =>
        new Response(
          JSON.stringify({ error: { code: "bad_credentials", message: "Неверная почта или пароль" } }),
          { status: 401 },
        ),
    });
    render(renderApp(<App />, "/login"));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Электронная почта"), "x@demo.ru");
    await user.type(screen.getByLabelText("Пароль"), "wrong");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByText("Неверная почта или пароль")).toBeInTheDocument();
  });
});

describe("шапка", () => {
  beforeEach(() => tokenStore.set("t1"));
  afterEach(() => {
    tokenStore.set(null);
    vi.unstubAllGlobals();
  });

  it("главная — плитки разделов; в шапке только знак и «Выйти»; «Качества» нет", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/"));
    expect(await screen.findByRole("heading", { name: "Главная" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Новая проверка/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Мои проверки/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Правила/ })).toBeNull(); // специалисту — нет
    expect(screen.queryByRole("link", { name: "Качество" })).toBeNull();
    // выход — сразу кнопкой, без выпадающего меню профиля
    expect(screen.getByRole("button", { name: /Выйти/ })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("link", { name: /Новая проверка/ }));
    expect(await screen.findByRole("heading", { name: "Новая проверка" })).toBeInTheDocument();
    // знак в шапке возвращает на главную
    await userEvent.setup().click(screen.getByRole("link", { name: /на главную/ }));
    expect(await screen.findByRole("heading", { name: "Главная" })).toBeInTheDocument();
  });

  it("кнопка «На главную» — под шапкой на всех страницах, кроме самой главной", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/new"));
    const home = await screen.findByRole("link", { name: "На главную" });
    await userEvent.setup().click(home);
    expect(await screen.findByRole("heading", { name: "Главная" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "На главную" })).toBeNull();
  });

  it("плитка «Мои проверки» ведёт на список проверок", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/"));
    await userEvent.setup().click(await screen.findByRole("link", { name: /Мои проверки/ }));
    expect(await screen.findByRole("heading", { name: "Проверки" })).toBeInTheDocument();
  });

  it("один набор правил — выбора набора на новой проверке нет", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/new"));
    expect(await screen.findByRole("heading", { name: "Новая проверка" })).toBeInTheDocument();
    expect(screen.queryByText("Набор правил")).toBeNull();
  });

  it("старый адрес «Качества» ведёт на главную", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/quality"));
    expect(await screen.findByRole("heading", { name: "Главная" })).toBeInTheDocument();
  });

  it("старый адрес «Истории» ведёт на список проверок", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME });
    render(renderApp(<App />, "/history"));
    expect(await screen.findByRole("heading", { name: "Проверки" })).toBeInTheDocument();
  });
});

describe("загрузка из ЕИС по номеру", () => {
  beforeEach(() => tokenStore.set("t1"));
  afterEach(() => {
    vi.unstubAllGlobals();
    tokenStore.set(null);
  });

  it("номер → документы скачиваются → открывается проверка", async () => {
    const { calls } = mockApi({
      "/api/auth/me": SPECIALIST,
      ...EMPTY_HOME,
      "POST /api/checks/eis": { task_id: "task1.sig" },
      "/api/checks/eis/task1.sig": { status: "done", check_id: "c1", package_id: "p1" },
    });
    render(renderApp(<App />, "/new"));
    const button = await screen.findByRole("button", { name: /Загрузить из ЕИС/ });
    const field = screen.getByRole("textbox", { name: "Реестровый номер закупки в ЕИС" });
    const user = userEvent.setup();
    await user.type(field, "123");
    expect(button).toBeDisabled(); // не 19 и не 11 цифр — запрос не уходит
    expect(screen.getByText(/19 цифр \(44-ФЗ\) или 11 цифр/)).toBeInTheDocument();
    await user.clear(field);
    await user.type(field, "№ 0362 1000 2612 6000 023");
    await user.click(button);
    await vi.waitFor(() => expect(calls).toContain("GET /api/checks/c1"), { timeout: 5000 });
    expect(calls).toContain("POST /api/checks/eis");
  });

  it("ошибка ЕИС — сообщение по-русски, страница остаётся", async () => {
    mockApi({
      "/api/auth/me": SPECIALIST,
      ...EMPTY_HOME,
      "POST /api/checks/eis": { task_id: "task2.sig" },
      "/api/checks/eis/task2.sig": { status: "failed", error: "ЕИС сейчас недоступна" },
    });
    render(renderApp(<App />, "/new"));
    const user = userEvent.setup();
    await user.type(
      await screen.findByRole("textbox", { name: "Реестровый номер закупки в ЕИС" }),
      "32616529449",
    );
    await user.click(screen.getByRole("button", { name: /Загрузить из ЕИС/ }));
    expect(await screen.findByText("ЕИС сейчас недоступна", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Новая проверка" })).toBeInTheDocument();
  });

  it("загрузка выключена на сервере — блока нет", async () => {
    mockApi({ "/api/auth/me": SPECIALIST, ...EMPTY_HOME, "/api/settings": { eis_fetch: false, limits: { max_upload_mb: 100 } } });
    render(renderApp(<App />, "/new"));
    expect(await screen.findByRole("heading", { name: "Новая проверка" })).toBeInTheDocument();
    await screen.findByText(/до 100 МБ/);
    expect(screen.queryByRole("button", { name: /Загрузить из ЕИС/ })).toBeNull();
  });
});

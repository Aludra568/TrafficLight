import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockApi, renderApp } from "../../test/render";
import { PortfolioEditTitle } from "./PortfolioEditTitle";

describe("PortfolioEditTitle", () => {
  it("сохраняет ввод и исходную версию при обновлении данных портфеля", async () => {
    const { calls } = mockApi({
      "PATCH /api/portfolios/p-1": (init?: RequestInit) => {
        expect(JSON.parse(init?.body as string)).toEqual({ title: "Мой вариант", version: 1 });
        return { id: "p-1", title: "Мой вариант", title_version: 3 };
      },
    });
    const view = render(renderApp(
      <PortfolioEditTitle portfolioId="p-1" title="Старое" version={1} canEdit />,
    ));
    fireEvent.click(screen.getByRole("button", { name: /переименовать/i }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Мой вариант" } });
    view.rerender(renderApp(
      <PortfolioEditTitle portfolioId="p-1" title="Чужое изменение" version={2} canEdit />,
    ));
    expect(screen.getByRole("textbox")).toHaveValue("Мой вариант");
    fireEvent.click(screen.getByRole("button", { name: /сохранить/i }));
    await waitFor(() => expect(calls).toContain("PATCH /api/portfolios/p-1"));
  });

  it("не показывает кнопку редактирования при canEdit=false", () => {
    render(
      renderApp(
        <PortfolioEditTitle
          portfolioId="p-1"
          title="Закупки ноября"
          version={1}
          canEdit={false}
        />,
      ),
    );
    expect(screen.getByText("Закупки ноября")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /переименовать/i })).not.toBeInTheDocument();
  });

  it("позволяет переименовать портфель и отправляет PATCH с версией", async () => {
    const { calls } = mockApi({
      "PATCH /api/portfolios/p-1": (init?: RequestInit) => {
        const body = JSON.parse(init?.body as string);
        expect(body).toEqual({ title: "Новое название", version: 1 });
        return {
          id: "p-1",
          title: "Новое название",
          title_version: 2,
          can_edit: true,
          items: [],
          repeated_risks: [],
          findings: [],
        };
      },
    });

    render(
      renderApp(
        <PortfolioEditTitle
          portfolioId="p-1"
          title="Старое название"
          version={1}
          canEdit={true}
        />,
      ),
    );

    const editBtn = screen.getByRole("button", { name: /переименовать/i });
    fireEvent.click(editBtn);

    const input = screen.getByRole("textbox", { name: /название портфеля/i });
    fireEvent.change(input, { target: { value: "Новое название" } });

    const saveBtn = screen.getByRole("button", { name: /сохранить/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(calls.some((c) => c.startsWith("PATCH /api/portfolios/p-1"))).toBe(true);
    });
  });

  it("корректно обрабатывает ошибку конфликта 409 при переименовании", async () => {
    mockApi({
      "PATCH /api/portfolios/p-1": () =>
        new Response(
          JSON.stringify({
            error: { code: "version_conflict", message: "Версия устарела" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    });

    render(
      renderApp(
        <PortfolioEditTitle
          portfolioId="p-1"
          title="Старое название"
          version={1}
          canEdit={true}
        />,
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: /переименовать/i }));
    fireEvent.change(screen.getByRole("textbox", { name: /название портфеля/i }), {
      target: { value: "Новое название" },
    });
    fireEvent.click(screen.getByRole("button", { name: /сохранить/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/конфликт/i),
      ).toBeInTheDocument();
    });
  });
});

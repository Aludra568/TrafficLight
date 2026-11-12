import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PortfolioItemOut } from "../../api/portfolios";
import { mockApi, renderApp } from "../../test/render";
import { PortfolioItems } from "./PortfolioItems";

const baseItems: PortfolioItemOut[] = [
  {
    id: "item-wait",
    position: 0,
    source_type: "file",
    source_name: "Закупка в очереди",
    check_id: null,
    status: "waiting",
    light: null,
    progress: 0,
    error: null,
    findings: 0,
    version: 1,
  },
  {
    id: "item-run",
    position: 1,
    source_type: "file",
    source_name: "Закупка в работе",
    check_id: null,
    status: "running",
    light: null,
    progress: 40,
    error: null,
    findings: 0,
    version: 1,
  },
  {
    id: "item-edit",
    position: 2,
    source_type: "file",
    source_name: "Закупка перепроверяется",
    check_id: "check-prev",
    status: "done",
    light: "yellow",
    progress: 100,
    error: null,
    findings: 2,
    version: 2,
    editing: true,
    edit_error: "Ошибка фоновой перепроверки",
  },
];

describe("PortfolioItems", () => {
  it("не показывает действия редактирования и удаления, если canEdit=false", () => {
    render(
      renderApp(
        <PortfolioItems items={baseItems} portfolioId="p-1" canEdit={false} />,
      ),
    );

    expect(screen.queryByRole("button", { name: /удалить позицию/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /состав документов/i })).not.toBeInTheDocument();
  });

  it("блокирует удаление при running и editing, но разрешает для waiting", async () => {
    render(
      renderApp(
        <PortfolioItems items={baseItems} portfolioId="p-1" canEdit={true} />,
      ),
    );

    const deleteButtons = screen.getAllByRole("button", { name: /удалить позицию/i });
    expect(deleteButtons).toHaveLength(3);

    // waiting -> enabled
    expect(deleteButtons[0]).not.toBeDisabled();
    // running -> disabled
    expect(deleteButtons[1]).toBeDisabled();
    // editing -> disabled
    expect(deleteButtons[2]).toBeDisabled();
  });

  it("отображает статус обновления и сохраняет ссылку на отчёт при editing=true", () => {
    render(
      renderApp(
        <PortfolioItems items={baseItems} portfolioId="p-1" canEdit={true} />,
      ),
    );

    expect(screen.getByText("Обновляется…")).toBeInTheDocument();
    expect(screen.getByText("Ошибка фоновой перепроверки")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /отчёт/i })).toHaveAttribute(
      "href",
      "/checks/check-prev",
    );
  });

  it("удаляет позицию с подтверждением и передаёт version в query", async () => {
    const { calls } = mockApi({
      "DELETE /api/portfolios/p-1/items/item-wait": new Response(null, { status: 204 }),
    });

    render(
      renderApp(
        <PortfolioItems items={baseItems} portfolioId="p-1" canEdit={true} />,
      ),
    );

    const deleteButtons = screen.getAllByRole("button", { name: /удалить позицию/i });
    fireEvent.click(deleteButtons[0]);

    // Появляется Popconfirm
    const confirmBtn = await screen.findByRole("button", { name: "Убрать" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(calls.some((c) => c.startsWith("DELETE /api/portfolios/p-1/items/item-wait?version=1"))).toBe(true);
    });
  });
});

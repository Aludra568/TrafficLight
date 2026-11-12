import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PortfolioOut } from "../../api/portfolios";
import { mockApi, renderApp } from "../../test/render";
import { PortfolioView } from "./PortfolioView";

vi.mock("../../lib/portfolioFilename", () => ({
  portfolioExportName: (title: string, kind: string) => `Чек-лист по «${title}».${kind}`,
}));

const mockPortfolio: PortfolioOut = {
  id: "p-view-1",
  title: "Тестовый портфель",
  created_at: "2026-11-01T12:00:00Z",
  light: "green",
  complete: true,
  total: 1,
  done: 1,
  running: 0,
  waiting: 0,
  failed: 0,
  distribution: { green: 1 },
  items: [
    {
      id: "it-1",
      position: 0,
      source_type: "file",
      source_name: "Закупка №1",
      check_id: "chk-1",
      status: "done",
      light: "green",
      progress: 100,
      error: null,
      findings: 0,
      version: 1,
      editing: false,
      edit_error: null,
    },
  ],
  repeated_risks: [],
  findings: [],
  title_version: 1,
  can_edit: true,
};

describe("PortfolioView", () => {
  it("скрывает кнопки редактирования при can_edit=false", async () => {
    mockApi({
      "GET /api/portfolios/p-readonly": {
        ...mockPortfolio,
        id: "p-readonly",
        can_edit: false,
      },
    });

    render(renderApp(<PortfolioView id="p-readonly" />));

    await waitFor(() => {
      expect(screen.getByText("Тестовый портфель")).toBeInTheDocument();
    });

    expect(screen.queryByRole("button", { name: /переименовать/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /добавить закупки/i })).not.toBeInTheDocument();
  });

  it("показывает кнопки редактирования при can_edit=true и открывает добавление", async () => {
    mockApi({
      "GET /api/portfolios/p-view-1": mockPortfolio,
    });

    render(renderApp(<PortfolioView id="p-view-1" />));

    await waitFor(() => {
      expect(screen.getByText("Тестовый портфель")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: /переименовать/i })).toBeInTheDocument();
    const addBtn = screen.getByRole("button", { name: /добавить закупки/i });
    expect(addBtn).toBeInTheDocument();

    fireEvent.click(addBtn);

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: /добавить закупки/i })).toBeInTheDocument();
    });
  });
});

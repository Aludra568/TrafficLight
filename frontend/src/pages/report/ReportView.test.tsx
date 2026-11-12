import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { CheckOut } from "../../api/types";
import { mockApi, renderApp } from "../../test/render";
import { ReportView } from "./ReportView";

vi.mock("../../components/PdfViewer", () => ({ PdfViewer: () => null }));
vi.mock("./ReportHeader", () => ({ ReportHeader: () => <div>Отчёт загружен</div> }));

afterEach(() => vi.unstubAllGlobals());

it("показывает ошибку отчёта и повторяет его загрузку той же кнопкой", async () => {
  let attempt = 0;
  const { calls } = mockApi({
    "/api/checks/c1/report": () => {
      attempt += 1;
      if (attempt === 1) return new Response(JSON.stringify({
        error: { code: "unavailable", message: "Сервис временно недоступен" },
      }), { status: 503 });
      return {
        findings: [], documents: [], card: [], rules: [],
        rules_summary: { checked: 0, not_checked: 0 },
      };
    },
  });
  render(renderApp(<ReportView check={{ id: "c1" } as CheckOut} />));
  expect(await screen.findByText("Не удалось загрузить отчёт")).toBeInTheDocument();
  expect(screen.getByText("Сервис временно недоступен")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку" }));
  expect(await screen.findByText("Отчёт загружен")).toBeInTheDocument();
  expect(calls.filter((c) => c === "GET /api/checks/c1/report")).toHaveLength(2);
});

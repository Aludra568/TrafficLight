import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FindingOut } from "../../api/types";
import { mockApi, renderApp } from "../../test/render";
import { HandlingControl } from "./HandlingControl";

const finding = { id: "finding-1", handling_status: "new", handling_version: 7 } as FindingOut;

afterEach(() => vi.unstubAllGlobals());

async function chooseResolved() {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "Статус обработки замечания" }));
  fireEvent.click(await screen.findByText("Отработано"));
}

describe("обработка замечания", () => {
  it("сохраняет выбранный статус с серверной версией", async () => {
    let body: unknown;
    mockApi({
      "PATCH /api/findings/finding-1/handling": (init?: RequestInit) => {
        body = JSON.parse(String(init?.body));
        return { status: "resolved", version: 8, updated_at: "2026-11-11T12:00:00Z" };
      },
    });
    render(renderApp(<HandlingControl finding={finding} checkId="check-1" />));
    await chooseResolved();
    await waitFor(() => expect(body).toEqual({ status: "resolved", version: 7 }));
    expect(await screen.findByText("Статус обработки сохранён")).toBeInTheDocument();
  });

  it("при конфликте объясняет ошибку, не показывает ложное сохранение", async () => {
    mockApi({
      "PATCH /api/findings/finding-1/handling": () => new Response(JSON.stringify({
        error: { code: "handling_conflict", message: "Статус уже изменён" },
      }), { status: 409 }),
    });
    render(renderApp(<HandlingControl finding={finding} checkId="check-1" />));
    await chooseResolved();
    expect(await screen.findByText(/Конфликт: статус замечания/)).toBeInTheDocument();
    expect(screen.queryByText("Статус обработки сохранён")).not.toBeInTheDocument();
  });
});

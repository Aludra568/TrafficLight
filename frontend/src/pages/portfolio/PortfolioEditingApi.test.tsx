import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import {
  downloadPortfolioDocx,
  downloadPortfolioExcel,
} from "../../api/portfolios";
import {
  useAddPortfolioItems,
  useItemDocuments,
  useRemovePortfolioItem,
  useRenamePortfolio,
  useUpdateItemDocuments,
} from "../../api/portfolioEditing";
import { mockApi, renderApp } from "../../test/render";

describe("portfolioEditing API hooks", () => {
  it("useRenamePortfolio отправляет PATCH и обрабатывает 409", async () => {
    let called = false;
    mockApi({
      "PATCH /api/portfolios/p-1": () => {
        called = true;
        return new Response(
          JSON.stringify({ error: { code: "conflict", message: "Устарела версия" } }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });

    const wrapper = ({ children }: { children: React.ReactNode }) => renderApp(children);
    const { result } = renderHook(() => useRenamePortfolio("p-1"), { wrapper });

    result.current.mutate({ title: "Тест", version: 1 });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });

    expect(called).toBe(true);
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect((result.current.error as ApiError).status).toBe(409);
  });

  it("useAddPortfolioItems отправляет FormData и обрабатывает 202", async () => {
    const captured: { body: FormData | null } = { body: null };
    mockApi({
      "POST /api/portfolios/p-1/items": (init?: RequestInit) => {
        captured.body = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1", dispatch_error: null }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const wrapper = ({ children }: { children: React.ReactNode }) => renderApp(children);
    const { result } = renderHook(() => useAddPortfolioItems("p-1"), { wrapper });

    const testFile = new File(["data"], "test.pdf");
    result.current.mutate({
      requestKey: "key-123",
      files: [testFile],
      bundle: false,
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(captured.body?.get("request_key")).toBe("key-123");
    expect(captured.body?.get("bundle")).toBe("false");
  });

  it("useRemovePortfolioItem отправляет DELETE с версией", async () => {
    const { calls } = mockApi({
      "DELETE /api/portfolios/p-1/items/it-1": new Response(null, { status: 204 }),
    });

    const wrapper = ({ children }: { children: React.ReactNode }) => renderApp(children);
    const { result } = renderHook(() => useRemovePortfolioItem("p-1"), { wrapper });

    result.current.mutate({ itemId: "it-1", version: 2 });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(calls.some((c) => c.includes("DELETE /api/portfolios/p-1/items/it-1?version=2"))).toBe(true);
  });

  it("useItemDocuments и useUpdateItemDocuments корректно работают с документами", async () => {
    const captured: { body: FormData | null } = { body: null };
    mockApi({
      "GET /api/portfolios/p-1/items/it-1/documents": {
        item_version: 5,
        editable: true,
        documents: [{ id: "d-1", filename: "f1.pdf", status: "ready" }],
      },
      "POST /api/portfolios/p-1/items/it-1/documents": (init?: RequestInit) => {
        captured.body = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const wrapper = ({ children }: { children: React.ReactNode }) => renderApp(children);
    const { result: queryResult } = renderHook(() => useItemDocuments("p-1", "it-1"), { wrapper });

    await waitFor(() => {
      expect(queryResult.current.isSuccess).toBe(true);
    });
    expect(queryResult.current.data?.documents).toHaveLength(1);

    const { result: mutationResult } = renderHook(() => useUpdateItemDocuments("p-1", "it-1"), { wrapper });
    mutationResult.current.mutate({
      requestKey: "req-abc",
      version: 5,
      removeIds: ["d-1"],
      files: [new File(["new"], "new.docx")],
    });

    await waitFor(() => {
      expect(mutationResult.current.isSuccess).toBe(true);
    });

    expect(captured.body?.get("request_key")).toBe("req-abc");
    expect(captured.body?.get("version")).toBe("5");
    expect(captured.body?.get("remove_ids")).toBe('["d-1"]');
  });

  it("скачивает Excel и Word с соответствующими названиями портфеля", async () => {
    const originalUrl = globalThis.URL;
    const names: string[] = [];
    const createObjectURL = vi.fn(() => "blob:portfolio-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", class extends originalUrl {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download);
    });
    vi.useFakeTimers();
    mockApi({
      "GET /api/portfolios/p-1/export.xlsx": () => new Response("x"),
      "GET /api/portfolios/p-1/export.docx": () => new Response("w"),
    });
    try {
      await downloadPortfolioExcel("p-1", "Закупки");
      await downloadPortfolioDocx("p-1", "Закупки");
      expect(names).toEqual(["Чек-лист по «Закупки».xlsx", "Отчёт по «Закупки».docx"]);
      expect(createObjectURL).toHaveBeenCalledTimes(2);
      await vi.runAllTimersAsync();
      expect(revokeObjectURL).toHaveBeenCalledTimes(2);
    } finally {
      click.mockRestore();
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});

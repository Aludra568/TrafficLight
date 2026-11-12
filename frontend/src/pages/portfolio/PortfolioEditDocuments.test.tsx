import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PortfolioItemOut } from "../../api/portfolios";
import { mockApi, renderApp } from "../../test/render";
import { PortfolioEditDocuments } from "./PortfolioEditDocuments";

const mockItem: PortfolioItemOut = {
  id: "item-1",
  position: 0,
  source_type: "file",
  source_name: "Закупка 1",
  check_id: "check-10",
  status: "done",
  light: "green",
  progress: 100,
  error: null,
  findings: 0,
  version: 3,
  editing: false,
  edit_error: null,
};

describe("PortfolioEditDocuments", () => {
  it("показывает документы, ссылку на отчёт и позволяет отправить Save&recheck", async () => {
    let capturedBody: FormData | null = null;
    mockApi({
      "GET /api/portfolios/p-1/items/item-1/documents": {
        item_version: 3,
        editable: true,
        documents: [
          { id: "doc-1", filename: "договор.pdf", status: "ready" },
          { id: "doc-2", filename: "спецификация.docx", status: "ready" },
        ],
      },
      "POST /api/portfolios/p-1/items/item-1/documents": (init?: RequestInit) => {
        capturedBody = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const view = render(
      renderApp(
        <PortfolioEditDocuments
          portfolioId="p-1"
          item={mockItem}
          open={true}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(screen.getByText("договор.pdf")).toBeInTheDocument();
      expect(screen.getByText("спецификация.docx")).toBeInTheDocument();
    });

    // Проверяем наличие ссылки на историю/отчёт
    expect(screen.getByRole("link", { name: /отчёт проверки/i })).toHaveAttribute(
      "href",
      "/checks/check-10",
    );

    // Удаляем один документ
    const deleteBtns = screen.getAllByRole("button", { name: /удалить документ/i });
    fireEvent.click(deleteBtns[0]);

    // Добавляем новый файл
    const newFile = new File(["new-spec"], "спецификация_нов.docx");
    const input = view.baseElement.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [newFile] } });

    await waitFor(() => {
      expect(screen.getByText("спецификация_нов.docx")).toBeInTheDocument();
    });

    // Нажимаем Сохранить и перепроверить
    const saveBtn = screen.getByRole("button", { name: /сохранить и перепроверить/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
      expect(capturedBody?.get("version")).toBe("3");
      expect(JSON.parse(capturedBody?.get("remove_ids") as string)).toEqual(["doc-1"]);
      expect(capturedBody?.getAll("files")).toHaveLength(1);
    });
  });

  it("показывает статус фонового редактирования и ошибку edit_error", async () => {
    mockApi({
      "GET /api/portfolios/p-1/items/item-1/documents": {
        item_version: 3,
        editable: false,
        documents: [],
      },
    });

    const itemEditing: PortfolioItemOut = {
      ...mockItem,
      editing: true,
      edit_error: "Текст ошибки подготовки",
    };

    render(
      renderApp(
        <PortfolioEditDocuments
          portfolioId="p-1"
          item={itemEditing}
          open={true}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(screen.getByText(/идёт подготовка и перепроверка/i)).toBeInTheDocument();
      expect(screen.getByText(/текст ошибки подготовки/i)).toBeInTheDocument();
      expect(screen.getByText(/нет документов/i)).toBeInTheDocument();
    });
  });

  it("показывает сообщение при конфликте 409", async () => {
    mockApi({
      "GET /api/portfolios/p-1/items/item-1/documents": {
        item_version: 1,
        editable: true,
        documents: [{ id: "doc-1", filename: "договор.pdf", status: "ready" }],
      },
      "POST /api/portfolios/p-1/items/item-1/documents": () =>
        new Response(
          JSON.stringify({ error: { code: "conflict", message: "Позиция устарела" } }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    });

    const view = render(
      renderApp(
        <PortfolioEditDocuments
          portfolioId="p-1"
          item={mockItem}
          open={true}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(screen.getByText("договор.pdf")).toBeInTheDocument();
    });

    const newFile = new File(["dummy"], "extra.pdf");
    const input = view.baseElement.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [newFile] } });

    await waitFor(() => {
      expect(screen.getByText("extra.pdf")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /сохранить и перепроверить/i }));

    await waitFor(() => {
      expect(screen.getByText(/конфликт/i)).toBeInTheDocument();
    });
  });
});

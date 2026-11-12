import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockApi, renderApp } from "../../test/render";
import { PortfolioEditAddItems } from "./PortfolioEditAddItems";

describe("PortfolioEditAddItems", () => {
  it("отправляет файлы отдельными закупками (bundle=false)", async () => {
    let capturedBody: FormData | null = null;
    mockApi({
      "POST /api/portfolios/p-1/items": (init?: RequestInit) => {
        capturedBody = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const file1 = new File(["content1"], "same.pdf", { type: "application/pdf" });
    const file2 = new File(["content2"], "same.pdf", { type: "application/pdf" });

    const view = render(
      renderApp(<PortfolioEditAddItems portfolioId="p-1" open={true} onClose={() => {}} />),
    );

    const input = view.baseElement.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [file1, file2] } });

    await waitFor(() => {
      expect(screen.getAllByText("same.pdf")).toHaveLength(2);
    });

    const submitBtn = screen.getByRole("button", { name: /добавить закупки/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
      expect(capturedBody?.get("bundle")).toBe("false");
      expect(capturedBody?.getAll("files")).toHaveLength(2);
      expect(capturedBody?.get("request_key")).toBeTruthy();
    });
  });

  it("отправляет закупку из нескольких файлов как один бандл (bundle=true)", async () => {
    let capturedBody: FormData | null = null;
    mockApi({
      "POST /api/portfolios/p-1/items": (init?: RequestInit) => {
        capturedBody = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const file1 = new File(["part1"], "contract.docx");
    const file2 = new File(["part2"], "specs.docx");

    const view = render(
      renderApp(<PortfolioEditAddItems portfolioId="p-1" open={true} onClose={() => {}} />),
    );

    const bundleRadio = screen.getByRole("radio", { name: /одна закупка/i });
    fireEvent.click(bundleRadio);

    const input = view.baseElement.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [file1, file2] } });

    await waitFor(() => {
      expect(screen.getByText("contract.docx")).toBeInTheDocument();
      expect(screen.getByText("specs.docx")).toBeInTheDocument();
    });

    const submitBtn = screen.getByRole("button", { name: /добавить закупки/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
      expect(capturedBody?.get("bundle")).toBe("true");
      expect(capturedBody?.getAll("files")).toHaveLength(2);
    });
  });

  it("отправляет номера ЕИС", async () => {
    let capturedBody: FormData | null = null;
    mockApi({
      "POST /api/portfolios/p-1/items": (init?: RequestInit) => {
        capturedBody = init?.body as FormData;
        return new Response(JSON.stringify({ id: "p-1" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    render(
      renderApp(<PortfolioEditAddItems portfolioId="p-1" open={true} onClose={() => {}} />),
    );

    const eisRadio = screen.getByRole("radio", { name: /номера еис/i });
    fireEvent.click(eisRadio);

    const textArea = screen.getByRole("textbox", { name: /номера закупок еис/i });
    fireEvent.change(textArea, {
      target: { value: "0362100026126000023\n32312345678" },
    });

    const submitBtn = screen.getByRole("button", { name: /добавить закупки/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
      expect(capturedBody?.get("numbers")).toBe("0362100026126000023\n32312345678");
    });
  });

  it("показывает сообщение о конфликте при 409", async () => {
    mockApi({
      "POST /api/portfolios/p-1/items": () =>
        new Response(
          JSON.stringify({ error: { code: "conflict", message: "Портфель изменён" } }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    });

    const file = new File(["c"], "doc.pdf");
    const view = render(
      renderApp(<PortfolioEditAddItems portfolioId="p-1" open={true} onClose={() => {}} />),
    );

    const input = view.baseElement.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByText("doc.pdf")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /добавить закупки/i }));

    await waitFor(() => {
      expect(screen.getByText(/конфликт/i)).toBeInTheDocument();
    });
  });
});

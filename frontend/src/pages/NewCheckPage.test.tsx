import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "../test/render";
import { NewCheckPage } from "./NewCheckPage";

afterEach(() => vi.unstubAllGlobals());

function show(limitMb = 100) {
  const submitted: FormData[] = [];
  mockApi({
    "/api/rulesets": [],
    "/api/settings": { limits: { max_upload_mb: limitMb } },
    "POST /api/checks": (init?: RequestInit) => {
      submitted.push(init?.body as FormData);
      return { check_id: "c1" };
    },
  });
  const view = render(renderApp(<NewCheckPage />));
  const choose = (files: File[]) => fireEvent.change(
    view.container.querySelector('input[type="file"]')!, { target: { files } },
  );
  return { choose, submitted };
}

it.each([false, true])("сохраняет одноимённые файлы, отдельные выборы: %s", async (separate) => {
  const first = new File(["one"], "Договор.txt", { lastModified: 1 });
  const second = new File(["two"], "Договор.txt", { lastModified: 1 });
  const { choose, submitted } = show();
  if (separate) {
    choose([first]);
    await screen.findByText("Договор.txt");
    choose([second]);
  } else choose([first, second]);
  const start = await screen.findByRole("button", { name: /Проверить 2 файла/ });
  fireEvent.click(start);
  await waitFor(() => expect(submitted).toHaveLength(1));
  const sent = submitted[0].getAll("files");
  expect(sent[0]).toBe(first);
  expect(sent[1]).toBe(second);
});

it("учитывает оба одноимённых файла в лимите и удаляет только выбранный", async () => {
  const first = new File([new Uint8Array(600_000)], "Договор.txt");
  const second = new File([new Uint8Array(600_000)], "Договор.txt");
  const { choose } = show(1);
  choose([first]);
  await screen.findByText("Договор.txt");
  choose([second]);
  const start = await screen.findByRole("button", { name: /Проверить 2 файла/ });
  await waitFor(() => expect(start).toBeDisabled());
  fireEvent.click(screen.getAllByRole("button", { name: "Убрать Договор.txt" })[0]);
  expect(await screen.findByRole("button", { name: /Проверить$/ })).toBeEnabled();
  expect(screen.getAllByText("Договор.txt")).toHaveLength(1);
});

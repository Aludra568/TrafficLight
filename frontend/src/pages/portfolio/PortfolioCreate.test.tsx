import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { renderApp } from "../../test/render";
import { PortfolioCreate } from "./PortfolioCreate";

const mutate = vi.hoisted(() => vi.fn());
vi.mock("../../api/portfolios", async (original) => ({
  ...await original<typeof import("../../api/portfolios")>(),
  useCreatePortfolio: () => ({ mutate, isPending: false }),
}));

it.each([false, true])("передаёт оба одноимённых пакета, отдельный выбор: %s", async (separate) => {
  mutate.mockClear();
  const first = new File(["one"], "same.zip");
  const second = new File(["two"], "same.zip");
  const view = render(renderApp(<PortfolioCreate />));
  const choose = (files: File[]) => fireEvent.change(
    view.container.querySelector('input[type="file"]')!, { target: { files } },
  );
  choose(separate ? [first] : [first, second]);
  await waitFor(() => expect(screen.getAllByText("same.zip")).toHaveLength(separate ? 1 : 2));
  if (separate) choose([second]);
  await waitFor(() => expect(screen.getAllByText("same.zip")).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: /Запустить проверку портфеля/ }));
  expect(mutate).toHaveBeenCalledTimes(1);
  expect(mutate.mock.calls[0][0].files).toEqual([first, second]);
});

import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { App } from "./App";

vi.mock("./auth/AuthProvider", () => ({
  useAuth: () => ({ user: null, loading: false }),
}));
vi.mock("./pages/LoginPage", () => ({
  LoginPage: () => {
    const location = useLocation();
    return <output>{(location.state as { from: string }).from}</output>;
  },
}));

it("сохраняет замечание, документ, страницу и hash при переходе на вход", async () => {
  const target = "/checks/check-1?finding=f2&document=d3&page=17#source";
  render(<MemoryRouter initialEntries={[target]}><App /></MemoryRouter>);
  expect(await screen.findByText(target)).toBeInTheDocument();
});

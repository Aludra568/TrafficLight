import { describe, expect, it } from "vitest";

import { safeReturnPath } from "./returnPath";

describe("safeReturnPath", () => {
  it("оставляет путь внутри сервиса", () => {
    expect(safeReturnPath("/checks/01a1?tab=risks")).toBe("/checks/01a1?tab=risks");
    expect(safeReturnPath("/")).toBe("/");
  });

  it("не уводит на чужой сайт", () => {
    for (const bad of ["//evil.example", "/\\evil.example", "https://evil.example", "evil", "/a\\b", "/\tx"]) {
      expect(safeReturnPath(bad)).toBe("/");
    }
  });

  it("без адреса — на главную", () => {
    expect(safeReturnPath(undefined)).toBe("/");
    expect(safeReturnPath({ pathname: "//x" })).toBe("/");
  });
});

import { render, screen } from "@testing-library/react";
import { Empty, Form, Input } from "antd";
import { describe, expect, it } from "vitest";

import { renderApp } from "../test/render";
import { count, documentPlace, plural } from "./format";
import { coverageText, LIGHT_LABELS, lightOf } from "./lights";

describe("склонение", () => {
  it.each([
    [1, "правило"],
    [2, "правила"],
    [5, "правил"],
    [11, "правил"],
    [21, "правило"],
    [22, "правила"],
    [112, "правил"],
  ])("%i → %s", (n, word) => {
    expect(plural(n, ["правило", "правила", "правил"])).toBe(word);
  });

  it("число со словом", () => {
    expect(count(3, ["файл", "файла", "файлов"])).toBe("3 файла");
  });
});

describe("полнота проверки", () => {
  it("«из N» — родительный падеж: из 1 и 21 правила, из 28 правил", () => {
    expect(coverageText(19, 28)).toBe("Проверено 19 из 28 правил");
    expect(coverageText(0, 21)).toBe("Проверено 0 из 21 правила");
    expect(coverageText(1, 1)).toBe("Проверено 1 из 1 правила");
    expect(coverageText(3, 11)).toBe("Проверено 3 из 11 правил");
  });

  it("неизвестный цвет — серый, а не зелёный", () => {
    expect(lightOf("gray")).toBe("gray");
    expect(lightOf(null)).toBe("gray");
    expect(lightOf("purple")).toBe("gray");
    expect(LIGHT_LABELS[lightOf("green")]).toBe("Нет рисков");
  });
});

describe("место цитаты", () => {
  it("заголовок документа в пути не повторяет имя файла", () => {
    expect(
      documentPlace(
        { section_path: "Проект контракта › 2. Цена контракта › 2.4", page_from: 3 },
        "Проект контракта.pdf",
      ),
    ).toBe("Проект контракта.pdf › 2. Цена контракта › 2.4 · стр. 3");
  });

  it("другой заголовок сохраняется; без страницы — без «стр.»", () => {
    expect(documentPlace({ section_path: "Раздел 1 › 1.2", page_from: null }, "ТЗ.docx")).toBe(
      "ТЗ.docx › Раздел 1 › 1.2",
    );
    expect(documentPlace({ section_path: null, page_from: 2 })).toBe(" · стр. 2");
  });
});

describe("локаль Ant Design", () => {
  it("русская: подписи «необязательно» и «Нет данных»", () => {
    render(
      renderApp(
        <>
          <Form requiredMark="optional">
            <Form.Item label="Комментарий" name="comment">
              <Input />
            </Form.Item>
          </Form>
          <Empty />
        </>,
      ),
    );
    expect(screen.getByText("(необязательно)")).toBeInTheDocument();
    expect(screen.getAllByText("Нет данных").length).toBeGreaterThan(0);
    expect(screen.queryByText("(optional)")).toBeNull();
  });
});

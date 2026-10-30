import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { emphasize } from "./emphasis";
import { shortPlace } from "./format";

const keys = (text: string) =>
  [...render(<p>{emphasize(text)}</p>).container.querySelectorAll(".key-value")].map((e) => e.textContent);

describe("значения в тексте", () => {
  it("выделяет число с единицей, включая пропись в скобках", () => {
    expect(
      keys("2.4. Оплата производится в течение 15 (пятнадцати) рабочих дней с даты приёмки."),
    ).toEqual(["15 (пятнадцати) рабочих дней"]);
    expect(keys("Срок поставки в ТЗ (30 календ. дн.) не совпадает (20 раб. дн.)")).toEqual([
      "30 календ. дн.",
      "20 раб. дн.",
    ]);
    expect(keys("обеспечение 5 % цены, НМЦК 1 890 000,00 ₽")).toEqual(["5 %", "1 890 000,00 ₽"]);
  });

  it("не трогает номера пунктов, годы и числа без единицы", () => {
    expect(keys("3.1. Закон от 05.04.2013 № 44-ФЗ, пункт 8.2")).toEqual([]);
  });

  it("слова не теряются", () => {
    const text = "Оплата через 15 раб. дн. после приёмки — дольше 7 раб. дн.";
    expect(render(<p>{emphasize(text)}</p>).container.textContent).toBe(text);
  });
});

describe("где цитата — коротко", () => {
  it("документ, пункт и страница вместо пути с разделами", () => {
    const where = { section_path: "Проект контракта › 2. ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ › 2.4", page_from: 1 };
    expect(shortPlace(where, "Проект контракта.pdf")).toBe("Проект контракта · п. 2.4 · стр. 1");
  });
});

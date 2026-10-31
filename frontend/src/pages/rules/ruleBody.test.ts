import { describe, expect, it } from "vitest";

import { toBody } from "./ruleBody";

const BASE = { id: "CON-01", title: "Срок", message: "м", severity: "red" };

describe("toBody", () => {
  it("сверка документов сохраняет своё поле fact", () => {
    const original = { ...BASE, type: "cross_document", fact: "delivery_term_days", compare: ["spec", "contract"] };
    const body = toBody(original, { title: "Срок поставки" });
    expect(body).toMatchObject({ fact: "delivery_term_days", compare: ["spec", "contract"], severity: "red", title: "Срок поставки" });
  });

  it("сверка внутри документа сохраняет список полей", () => {
    const original = { ...BASE, type: "internal_consistency", facts: ["quantity_total"] };
    expect(toBody(original, {}).facts).toEqual(["quantity_total"]);
  });

  it("смысловая проверка не уносит поля порогового правила", () => {
    const body = toBody({ ...BASE, type: "llm_check", fact: "x", conditions: [] }, {});
    expect(body.fact).toBeUndefined();
    expect(body.conditions).toBeUndefined();
    expect(body.group).toBe("custom");
  });

  it("у порогового правила цвет задают условия, а не общее поле", () => {
    const body = toBody({ ...BASE, type: "threshold", fact: "payment_term_days", conditions: [{ when: "value > 7", severity: "red" }] }, {});
    expect(body.severity).toBeUndefined();
    expect(body.fact).toBe("payment_term_days");
  });

  it("у не-смыслового правила examples удаляется", () => {
    const original = {
      ...BASE,
      type: "pattern",
      examples: [{ text: "исходный пример", verdict: "violation", why: "почему" }],
    };
    const body = toBody(original, {
      phrases: "поставка",
      examples: [{ text: "новый пример", verdict: "ok", why: "почему" }],
    });
    expect(body.examples).toBeUndefined();
  });

  it("у llm_check примеры из формы попадают в тело как есть", () => {
    const examples = [
      { text: "Поставка в выходные дни допускается по согласованию сторон.", verdict: "violation", why: "выходные дни" },
      { text: "Поставка осуществляется исключительно в рабочие дни.", verdict: "ok", why: "только рабочие дни" },
    ];
    const body = toBody({ ...BASE, type: "llm_check" }, { examples });
    expect(body.examples).toEqual(examples);
  });

  it("у llm_check пустой список примеров или с undefined очищается от пустых элементов", () => {
    const body = toBody({ ...BASE, type: "llm_check" }, { examples: [undefined] });
    expect(body.examples).toEqual([]);
  });
});

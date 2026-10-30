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
});

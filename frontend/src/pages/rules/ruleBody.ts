import { phrasesToPatterns } from "../../lib/catalog";

export type Body = Record<string, unknown>;

/** Тело правила для API: значения формы поверх исходного правила, без полей чужого типа. */
export function toBody(original: Body, values: Body): Body {
  const { phrases, excludes, ...rest } = values as Body & { phrases?: string; excludes?: string };
  const body: Body = { ...original, ...rest };
  if (body.type === "pattern") {
    body.patterns = phrasesToPatterns(phrases ?? "");
    body.exclude_patterns = phrasesToPatterns(excludes ?? "");
    body.scope = body.scope ?? "chunk";
  }
  if (body.type !== "llm_check") {
    for (const key of ["group", "question", "violation_if", "ok_if", "search_hints", "absent_behavior", "absent_severity", "absent_message", "examples"]) {
      delete body[key];
    }
  }
  if (body.type !== "pattern") {
    delete body.patterns;
    delete body.exclude_patterns;
    delete body.scope;
  }
  if (body.type !== "threshold") {
    // у сверки документов поле fact своё — без него правило не сохранится
    if (body.type !== "cross_document") delete body.fact;
    delete body.conditions;
    delete body.on_missing;
  } else {
    delete body.severity;
  }
  if (body.type === "llm_check") {
    if (!body.group) body.group = "custom";
    if (Array.isArray(body.examples)) {
      body.examples = (body.examples as unknown[]).filter((item) => item !== undefined && item !== null);
    } else if ("examples" in body && body.examples === undefined) {
      delete body.examples;
    }
  }
  return body;
}

/** Справочники правил: категории и условия карточки закупки (как в бэкенде, rules/model.py). */
export const CATEGORIES: Record<string, string> = {
  terms: "Сроки",
  payment: "Оплата и аванс",
  security: "Обеспечение",
  liability: "Неустойки и ответственность",
  termination: "Расторжение и подписание",
  acceptance: "Приёмка",
  requirements: "Требования к участнику",
  subject: "Предмет закупки",
  volume: "Объём и логистика",
  warranty: "Гарантия",
  national_regime: "Нацрежим",
  consistency: "Противоречия в документах",
};

export const NUMERIC_FACTS: { value: string; label: string; unit: string }[] = [
  { value: "payment_term_working_days", label: "Срок оплаты", unit: "раб. дн." },
  { value: "acceptance_term_working_days", label: "Срок приёмки", unit: "раб. дн." },
  { value: "delivery_term_days", label: "Срок поставки", unit: "дн." },
  { value: "advance_percent", label: "Аванс", unit: "%" },
  { value: "bid_security_percent", label: "Обеспечение заявки", unit: "%" },
  { value: "contract_security_percent", label: "Обеспечение исполнения", unit: "%" },
  { value: "warranty_security_percent", label: "Обеспечение гарантийных обязательств", unit: "%" },
  { value: "warranty_months", label: "Гарантийный срок", unit: "мес." },
  { value: "nmck_rub", label: "НМЦК", unit: "₽" },
  { value: "quantity_total", label: "Количество", unit: "" },
];

export const RULE_TYPES: Record<string, string> = {
  llm_check: "Смысловая проверка (модель)",
  pattern: "Поиск формулировок",
  threshold: "Порог по условию закупки",
  cross_document: "Сверка документов",
  internal_consistency: "Сверка внутри документа",
  signal: "Оформление файла",
};

/** Фразы простым текстом → регулярное выражение: без учёта регистра, любые пробелы между словами. */
export function phrasesToPatterns(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const words = line.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      return `(?i)${words.join("\\s+")}`;
    });
}

/** Обратно: простое выражение из фраз → фраза (для редактирования); сложное — как есть. */
export function patternToPhrase(pattern: string): string {
  const body = pattern.replace(/^\(\?i\)/, "");
  if (/[()[\]|*+?{}]/.test(body.replace(/\\s\+/g, " ").replace(/\\./g, ""))) return pattern;
  return body.replace(/\\s\+/g, " ").replace(/\\(.)/g, "$1");
}

export const DOC_TYPES = [
  { value: "notice", label: "Извещение" },
  { value: "contract", label: "Проект контракта" },
  { value: "spec", label: "Описание объекта закупки (ТЗ)" },
  { value: "nmck", label: "Обоснование НМЦК" },
  { value: "bid_requirements", label: "Требования к заявке" },
  { value: "other", label: "Другое" },
];

export const KIND_LABELS: Record<"violation" | "absent" | "contradiction", string> = {
  violation: "Условие найдено",
  absent: "Условие не найдено",
  contradiction: "Противоречие документов",
};

import type { LightKey } from "../theme";

/** Слово, иконка и цвет — смысл не держится на одном цвете (ч/б печать, дальтонизм). */
export const LIGHT_LABELS: Record<LightKey, string> = {
  red: "Критично",
  yellow: "Внимание",
  green: "Нет рисков",
  // серый — у пакета: замечаний нет, но проверена меньшая часть правил
  gray: "Не оценено",
};

export function lightOf(value: string | null | undefined): LightKey {
  return value === "red" || value === "yellow" || value === "green" ? value : "gray";
}

/**
 * Сколько правил проверено по существу. «Не проверено» и «в порядке» со слов модели без
 * подтверждённой цитаты в полноту не входят — так же считает бэкенд для серого цвета.
 */
export function assessedOf(summary: { checked: number; not_checked: number; unconfirmed?: number }): number {
  return summary.checked - summary.not_checked - (summary.unconfirmed ?? 0);
}

/** «Проверено 26 из 28 правил»: из 1 / 21 правила, из 2 / 5 / 28 правил. */
export function coverageText(assessed: number, total: number): string {
  const forms = total % 10 === 1 && total % 100 !== 11 ? "правила" : "правил";
  return `Проверено ${assessed} из ${total} ${forms}`;
}

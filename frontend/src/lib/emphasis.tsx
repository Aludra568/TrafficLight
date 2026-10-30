import type { ReactNode } from "react";

/**
 * Значения в тексте: число с единицей («15 рабочих дней», «30 (тридцати) календарных дней»,
 * «5 %», «1 890 000,00 ₽», «36 шт.»). Без единицы число не выделяется: номера пунктов «2.4.»
 * и годы в реквизитах — не суть замечания. В JS `\w` не ловит кириллицу, поэтому — [а-яё].
 */
const UNIT = String.raw`(?:календарн[а-яё]*\s+дн[а-яё]*|рабоч[а-яё]*\s+дн[а-яё]*|календ\.\s*дн\.|раб\.\s*дн\.|дн\.|дн(?:я|ей|ь)|суток|сутки|месяц[а-яё]*|недел[а-яё]*|год[а-яё]*|лет|час[а-яё]*|процент[а-яё]*|%|₽|руб[а-яё]*\.?|шт\.?|штук[а-яё]*)`;
const VALUE = new RegExp(
  String.raw`\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?(?:\s*\([^)]{1,40}\))?\s*${UNIT}(?![а-яё])`,
  "gi",
);

/** Текст с выделенными значениями: глазу есть за что зацепиться, слова при этом те же. */
export function emphasize(text: string, className = "key-value"): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(VALUE)) {
    const start = match.index ?? 0;
    if (start > last) parts.push(text.slice(last, start));
    parts.push(
      <span key={start} className={className}>
        {match[0]}
      </span>,
    );
    last = start + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

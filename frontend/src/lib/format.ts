import dayjs from "dayjs";
import "dayjs/locale/ru";
import relativeTime from "dayjs/plugin/relativeTime";

dayjs.extend(relativeTime);
dayjs.locale("ru");

/** 1 правило · 2 правила · 5 правил. */
export function plural(n: number, forms: [string, string, string]): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

export function count(n: number, forms: [string, string, string]): string {
  return `${n} ${plural(n, forms)}`;
}

/** «сегодня, 14:20», «вчера, 09:05», «12 сент., 16:40». */
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = dayjs(iso);
  const today = dayjs().startOf("day");
  if (date.isAfter(today)) return `сегодня, ${date.format("HH:mm")}`;
  if (date.isAfter(today.subtract(1, "day"))) return `вчера, ${date.format("HH:mm")}`;
  return date.format(date.year() === dayjs().year() ? "D MMM, HH:mm" : "D MMM YYYY");
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds % 60)} с`;
}

export function megabytes(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} МБ`;
}

/**
 * Коротко, где цитата: «Проект контракта · п. 2.4 · стр. 1». Полный путь с разделами в верхнем
 * регистре (`documentPlace`) — в подсказке: на каждой карточке он был самой громкой строкой.
 */
export function shortPlace(
  where: { section_path?: string | null; page_from?: number | null },
  filename?: string,
): string {
  const parts = (where.section_path ?? "").split(" › ").filter(Boolean);
  const last = parts[parts.length - 1] ?? "";
  const number = last.match(/^(\d+(?:\.\d+)*)\.?(?:\s|$)/)?.[1];
  const section = number
    ? `п. ${number}`
    : last && parts.length > 1
      ? last.charAt(0) + last.slice(1).toLowerCase()
      : "";
  const name = filename?.replace(/\.[^.]+$/, "");
  return [name, section, where.page_from ? `стр. ${where.page_from}` : ""].filter(Boolean).join(" · ");
}

/** «Проект контракта.pdf › 2. Цена… › 2.4 · стр. 1»: заголовок документа в пути не повторяет имя файла. */
export function documentPlace(
  where: { section_path?: string | null; page_from?: number | null },
  filename?: string,
): string {
  const parts = (where.section_path ?? "").split(" › ").filter(Boolean);
  const stem = filename?.replace(/\.[^.]+$/, "").toLowerCase();
  if (stem && parts.length && stem.startsWith(parts[0].toLowerCase())) parts.shift();
  const path = [filename, ...parts].filter(Boolean).join(" › ");
  return `${path}${where.page_from ? ` · стр. ${where.page_from}` : ""}`;
}

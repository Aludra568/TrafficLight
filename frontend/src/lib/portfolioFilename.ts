/**
 * Формирование безопасных имён файлов для выгрузок портфелей (Excel и Word).
 */

export type PortfolioExportKind = "xlsx" | "docx";

export const FALLBACK_PORTFOLIO_TITLE = "Портфель";
export const MAX_PORTFOLIO_TITLE_LENGTH = 120;

/**
 * Очищает название портфеля для безопасного использования в имени файла:
 * - Заменяет недопустимые символы Windows (<>:"/\|?*) и управляющие символы (включая CRLF) на '_'
 * - Слэши заменяются на '_', часть названия не отбрасывается как путь
 * - Удаляет пробелы и точки по краям
 * - Ограничивает длину 120 кодовыми точками Unicode (astral Unicode)
 * - При пустом результате возвращает 'Портфель'
 */
export function cleanPortfolioTitle(title: string | null | undefined): string {
  if (!title) {
    return FALLBACK_PORTFOLIO_TITLE;
  }

  // eslint-disable-next-line no-control-regex -- управляющие символы Windows и ASCII отсекаются в имени файла
  const replaced = title.replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/gu, "_");

  // Удаляем пробелы и точки по краям
  const stripped = replaced.replace(/^[ .]+|[ .]+$/g, "");

  // Ограничиваем длину 120 кодовыми точками Unicode (Array.from учитывает суррогатные пары)
  const codepoints = Array.from(stripped);
  const truncated = codepoints.slice(0, MAX_PORTFOLIO_TITLE_LENGTH).join("");

  return truncated || FALLBACK_PORTFOLIO_TITLE;
}

/**
 * Имя файла выгрузки портфеля по формату:
 * - xlsx: «Чек-лист по «НАЗВАНИЕ».xlsx»
 * - docx: «Отчёт по «НАЗВАНИЕ».docx»
 */
export function portfolioExportName(
  title: string,
  kind: "xlsx" | "docx",
): string {
  const norm = String(kind).trim().toLowerCase().replace(/^\./, "");
  let prefix: string;
  if (norm === "xlsx") {
    prefix = "Чек-лист по";
  } else if (norm === "docx") {
    prefix = "Отчёт по";
  } else {
    throw new Error(
      `Недопустимый тип выгрузки: "${kind}". Разрешены только "xlsx" и "docx".`,
    );
  }

  const safeTitle = cleanPortfolioTitle(title);
  return `${prefix} «${safeTitle}».${norm}`;
}

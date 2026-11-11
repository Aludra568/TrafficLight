export const LIMITS = {
  maxFiles: 20,
  maxFileMb: 100,
  maxTotalMb: 300,
} as const;

export function formatBytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(1).replace(".0", "")} МБ`;
}

export function validatePortfolioFiles(files: File[]): { valid: boolean; error?: string } {
  if (files.length === 0) {
    return { valid: false, error: "Выберите хотя бы один файл закупки" };
  }
  if (files.length > LIMITS.maxFiles) {
    return { valid: false, error: `Превышен лимит файлов: максимум ${LIMITS.maxFiles}` };
  }
  const maxFileBytes = LIMITS.maxFileMb * 1024 * 1024;
  if (files.some((f) => f.size === 0)) {
    return { valid: false, error: "Один из выбранных файлов пустой" };
  }
  const oversized = files.find((f) => f.size > maxFileBytes);
  if (oversized) {
    return {
      valid: false,
      error: `Файл «${oversized.name}» (${formatBytes(oversized.size)}) превышает лимит ${LIMITS.maxFileMb} МБ`,
    };
  }
  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);
  const maxTotalBytes = LIMITS.maxTotalMb * 1024 * 1024;
  if (totalBytes > maxTotalBytes) {
    return {
      valid: false,
      error: `Общий размер файлов (${formatBytes(totalBytes)}) превышает лимит ${LIMITS.maxTotalMb} МБ`,
    };
  }
  return { valid: true };
}

export interface EisParseResult {
  valid: string[];
  duplicates: string[];
  invalid: string[];
}

/** Проверка номеров ЕИС: 19 цифр (44-ФЗ) или 11 цифр (223-ФЗ). Пустые и ошибочные строки блокируют запуск. */
export function parseEisNumbers(rawText: string): EisParseResult {
  const lines = rawText.split(/\r?\n/);
  const valid: string[] = [];
  const duplicates: string[] = [];
  const invalid: string[] = [];
  const seen = new Set<string>();

  for (let i = 0; i < lines.length; i += 1) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (!trimmed) {
      invalid.push(`Строка ${i + 1}: пустая строка`);
      continue;
    }
    const digits = trimmed.replace(/[\s№#]/g, "");
    if (/^(\d{19}|\d{11})$/.test(digits)) {
      if (seen.has(digits)) {
        if (!duplicates.includes(digits)) duplicates.push(digits);
      } else {
        seen.add(digits);
        valid.push(digits);
      }
    } else {
      invalid.push(`Строка ${i + 1}: «${trimmed}» (требуется 19 или 11 цифр)`);
    }
  }

  return { valid, duplicates, invalid };
}

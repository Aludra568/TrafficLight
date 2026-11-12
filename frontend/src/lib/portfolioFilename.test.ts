import { describe, expect, it } from "vitest";

import {
  cleanPortfolioTitle,
  FALLBACK_PORTFOLIO_TITLE,
  MAX_PORTFOLIO_TITLE_LENGTH,
  portfolioExportName,
} from "./portfolioFilename";

describe("portfolioExportName и cleanPortfolioTitle", () => {
  it("обрабатывает обычные русские названия для xlsx и docx", () => {
    const title = "Закупка оборудования 2026";
    expect(portfolioExportName(title, "xlsx")).toBe(
      "Чек-лист по «Закупка оборудования 2026».xlsx",
    );
    expect(portfolioExportName(title, "docx")).toBe(
      "Отчёт по «Закупка оборудования 2026».docx",
    );
  });

  it("заменяет запрещённые в Windows символы (<>:\"/\\|?*) на подчёркивание", () => {
    const title = 'План <1>: "Лот" / 2026 \\ Часть | ? *';
    const expected = "План _1__ _Лот_ _ 2026 _ Часть _ _ _";
    expect(cleanPortfolioTitle(title)).toBe(expected);
    expect(portfolioExportName(title, "xlsx")).toBe(
      `Чек-лист по «${expected}».xlsx`,
    );
  });

  it("не отбрасывает часть названия как путь каталога при наличии слэшей", () => {
    const title = "каталог/подкаталог\\файл";
    expect(cleanPortfolioTitle(title)).toBe("каталог_подкаталог_файл");
    expect(portfolioExportName(title, "docx")).toBe(
      "Отчёт по «каталог_подкаталог_файл».docx",
    );
  });

  it("заменяет управляющие символы ASCII (0x00-0x1F, 0x7F) и CRLF на подчёркивание", () => {
    const title = "Строка 1\r\nСтрока 2\t\x00\x1f\x7fКонец";
    const expected = "Строка 1__Строка 2____Конец";
    expect(cleanPortfolioTitle(title)).toBe(expected);
    expect(portfolioExportName(title, "xlsx")).toBe(
      `Чек-лист по «${expected}».xlsx`,
    );
  });

  it("удаляет пробелы и точки только по краям, сохраняя их внутри названия", () => {
    const title = " . . . Проект 1.0 от 12.11 . . . ";
    const expected = "Проект 1.0 от 12.11";
    expect(cleanPortfolioTitle(title)).toBe(expected);
    expect(portfolioExportName(title, "docx")).toBe(
      `Отчёт по «${expected}».docx`,
    );
  });

  it("при пустом названии, одних пробелах или точках возвращает fallback «Портфель»", () => {
    expect(cleanPortfolioTitle("")).toBe(FALLBACK_PORTFOLIO_TITLE);
    expect(cleanPortfolioTitle("   ")).toBe(FALLBACK_PORTFOLIO_TITLE);
    expect(cleanPortfolioTitle("...")).toBe(FALLBACK_PORTFOLIO_TITLE);
    expect(cleanPortfolioTitle(" . . . ")).toBe(FALLBACK_PORTFOLIO_TITLE);
    expect(cleanPortfolioTitle(null)).toBe(FALLBACK_PORTFOLIO_TITLE);
    expect(cleanPortfolioTitle(undefined)).toBe(FALLBACK_PORTFOLIO_TITLE);

    expect(portfolioExportName("", "xlsx")).toBe("Чек-лист по «Портфель».xlsx");
    expect(portfolioExportName("   ", "docx")).toBe("Отчёт по «Портфель».docx");
    expect(portfolioExportName("...", "xlsx")).toBe("Чек-лист по «Портфель».xlsx");
  });

  it("ограничивает длину названия ровно 120 кодовыми точками Unicode", () => {
    const longTitle = "А".repeat(150);
    const cleaned = cleanPortfolioTitle(longTitle);
    expect(Array.from(cleaned).length).toBe(MAX_PORTFOLIO_TITLE_LENGTH);
    expect(cleaned).toBe("А".repeat(120));
    expect(portfolioExportName(longTitle, "xlsx")).toBe(
      `Чек-лист по «${"А".repeat(120)}».xlsx`,
    );

    const exact120 = "Б".repeat(120);
    expect(cleanPortfolioTitle(exact120)).toBe(exact120);
  });

  it("сохраняет astral Unicode (эмодзи) и считает их как 1 кодовую точку", () => {
    const title = "Портфель 🚀📁🦄";
    expect(cleanPortfolioTitle(title)).toBe("Портфель 🚀📁🦄");
    expect(portfolioExportName(title, "xlsx")).toBe(
      "Чек-лист по «Портфель 🚀📁🦄».xlsx",
    );

    // 130 эмодзи обрезаются ровно до 120 кодовых точек (без расщепления суррогатных пар)
    const astralLong = "🚀".repeat(130);
    const cleaned = cleanPortfolioTitle(astralLong);
    expect(Array.from(cleaned).length).toBe(120);
    expect(cleaned).toBe("🚀".repeat(120));
    expect(portfolioExportName(astralLong, "docx")).toBe(
      `Отчёт по «${"🚀".repeat(120)}».docx`,
    );
  });

  it("разрешает только xlsx и docx, на неизвестные типы выбрасывает ошибку", () => {
    const title = "Тест";
    expect(portfolioExportName(title, "xlsx")).toBe("Чек-лист по «Тест».xlsx");
    expect(portfolioExportName(title, "docx")).toBe("Отчёт по «Тест».docx");

    for (const badKind of ["pdf", "csv", "exe", "txt", ""] as unknown as ("xlsx" | "docx")[]) {
      expect(() => portfolioExportName(title, badKind)).toThrow(
        /Недопустимый тип выгрузки/,
      );
    }
  });

  it("соответствует одинаковым контрольным векторам бэкенда", () => {
    const vectors: [string, "xlsx" | "docx", string][] = [
      [
        "Аукцион на поставку оргтехники",
        "xlsx",
        "Чек-лист по «Аукцион на поставку оргтехники».xlsx",
      ],
      [
        "Аукцион на поставку оргтехники",
        "docx",
        "Отчёт по «Аукцион на поставку оргтехники».docx",
      ],
      [
        "отдел/закупки/2026\\лот:1",
        "xlsx",
        "Чек-лист по «отдел_закупки_2026_лот_1».xlsx",
      ],
      [
        "Документ <1> *проект*?\r\nверсия|2",
        "docx",
        "Отчёт по «Документ _1_ _проект____версия_2».docx",
      ],
      [
        "Тендер 🏆 2026",
        "xlsx",
        "Чек-лист по «Тендер 🏆 2026».xlsx",
      ],
      [
        "   ...   ",
        "xlsx",
        "Чек-лист по «Портфель».xlsx",
      ],
      [
        "",
        "docx",
        "Отчёт по «Портфель».docx",
      ],
    ];

    for (const [title, kind, expected] of vectors) {
      expect(portfolioExportName(title, kind)).toBe(expected);
    }
  });
});

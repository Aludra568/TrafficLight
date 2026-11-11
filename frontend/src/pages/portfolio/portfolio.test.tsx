import { describe, expect, it } from "vitest";

import { parseEisNumbers, validatePortfolioFiles } from "./portfolioUtils";

function createMockFile(name: string, sizeBytes: number): File {
  return {
    name,
    size: sizeBytes,
    type: "application/pdf",
  } as unknown as File;
}

describe("validatePortfolioFiles", () => {
  it("отклоняет пустой список файлов", () => {
    const res = validatePortfolioFiles([]);
    expect(res.valid).toBe(false);
    expect(res.error).toContain("хотя бы один файл");
  });

  it("отклоняет больше 20 файлов", () => {
    const files = Array.from({ length: 21 }, (_, i) => createMockFile(`file_${i}.pdf`, 1024));
    const res = validatePortfolioFiles(files);
    expect(res.valid).toBe(false);
    expect(res.error).toContain("максимум 20");
  });

  it("отклоняет отдельный файл более 100 МБ", () => {
    const bigFile = createMockFile("huge.zip", 101 * 1024 * 1024);
    const res = validatePortfolioFiles([bigFile]);
    expect(res.valid).toBe(false);
    expect(res.error).toContain("превышает лимит 100 МБ");
  });

  it("отклоняет суммарный размер файлов более 300 МБ", () => {
    const files = [
      createMockFile("part1.zip", 90 * 1024 * 1024),
      createMockFile("part2.zip", 90 * 1024 * 1024),
      createMockFile("part3.zip", 90 * 1024 * 1024),
      createMockFile("part4.zip", 50 * 1024 * 1024),
    ];
    const res = validatePortfolioFiles(files);
    expect(res.valid).toBe(false);
    expect(res.error).toContain("превышает лимит 300 МБ");
  });

  it("пропускает корректный список файлов", () => {
    const files = [
      createMockFile("procurement_1.pdf", 5 * 1024 * 1024),
      createMockFile("procurement_2.zip", 25 * 1024 * 1024),
    ];
    const res = validatePortfolioFiles(files);
    expect(res.valid).toBe(true);
    expect(res.error).toBeUndefined();
  });
});

describe("parseEisNumbers", () => {
  it("распознаёт корректные номера 19 цифр (44-ФЗ) и 11 цифр (223-ФЗ)", () => {
    const text = "0362100026126000023\n32312345678";
    const res = parseEisNumbers(text);
    expect(res.valid).toEqual(["0362100026126000023", "32312345678"]);
    expect(res.duplicates).toEqual([]);
    expect(res.invalid).toEqual([]);
  });

  it("очищает спецсимволы пробелов и номера №", () => {
    const text = "№ 0362 1000 2612 6000 023";
    const res = parseEisNumbers(text);
    expect(res.valid).toEqual(["0362100026126000023"]);
    expect(res.invalid).toEqual([]);
  });

  it("выделяет дубликаты и исключает их из валидного списка", () => {
    const text = "0362100026126000023\n0362100026126000023\n32312345678";
    const res = parseEisNumbers(text);
    expect(res.valid).toEqual(["0362100026126000023", "32312345678"]);
    expect(res.duplicates).toEqual(["0362100026126000023"]);
    expect(res.invalid).toEqual([]);
  });

  it("фиксирует пустые строки и неверные форматы для блокировки запуска", () => {
    const text = "0362100026126000023\n\n12345";
    const res = parseEisNumbers(text);
    expect(res.valid).toEqual(["0362100026126000023"]);
    expect(res.invalid.length).toBe(2);
    expect(res.invalid[0]).toContain("пустая строка");
    expect(res.invalid[1]).toContain("требуется 19 или 11 цифр");
  });
});

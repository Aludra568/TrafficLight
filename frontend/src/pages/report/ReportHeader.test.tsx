import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CheckOut, ReportOut } from "../../api/types";
import { ErrorBoundary } from "../../components/ErrorBoundary";
import { mockApi, renderApp } from "../../test/render";
import { ReportHeader } from "./ReportHeader";

function report(over: Partial<ReportOut["rules_summary"]>, findings: Array<{ severity: string }> = []) {
  return {
    package: { title: "Поставка бумаги", source_filename: "Поставка бумаги.zip" },
    findings,
    card: [],
    documents: [],
    rules: [],
    risk: { index: null, contributions: {}, recommendation_text: null },
    rules_summary: { checked: 28, violations: 0, ok: 0, absent: 0, not_checked: 0, failed: 0, ...over },
  } as unknown as ReportOut;
}

function check(light: string, counters: Record<string, unknown> = {}) {
  return {
    id: "c1",
    light,
    status: "done",
    finished_at: null,
    duration_s: 12,
    counters,
  } as unknown as CheckOut;
}

function show(light: string, data: ReportOut, counters: Record<string, unknown> = {}) {
  mockApi({ "/api/checks/c1/editions": [] });
  render(
    renderApp(<ReportHeader check={check(light, counters)} report={data} onOpenTab={() => undefined} />),
  );
}

/** Причины «проверка неполная» свёрнуты: сразу видна одна строка, список — по кнопке. */
function details() {
  fireEvent.click(screen.getByRole("button", { name: "Подробнее" }));
}

describe("итог отчёта", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("серый: «риски не оценены», а не «рисков нет»", () => {
    show("gray", report({ not_checked: 28 }));
    expect(screen.getByText("Риски не оценены")).toBeInTheDocument();
    expect(screen.getByText(/Проверено 0 из 28 правил — этого мало/)).toBeInTheDocument();
    expect(screen.queryByText("Рисков не найдено")).toBeNull();
  });

  it("зелёный при непроверенных правилах — с оговоркой о полноте", () => {
    show("green", report({ ok: 26, not_checked: 2 }));
    expect(screen.getByText("Рисков не найдено")).toBeInTheDocument();
    expect(screen.getByText(/Проверено 26 из 28 правил/)).toBeInTheDocument();
  });

  it("нечитаемый текст, указания программе и «ok» без цитаты — видны и не входят в полноту", () => {
    show("green", report({ ok: 24, not_checked: 3, unconfirmed: 2 }), {
      rules_blocked: { unreadable: 2, instructions: 1 },
      vector_search: "unavailable",
    });
    details();
    expect(screen.getByText(/Проверено 23 из 28 правил/)).toBeInTheDocument(); // 28 − 3 − 2
    expect(screen.getByText(/2 правила не проверено: текст прочитан не полностью/)).toBeInTheDocument();
    expect(screen.getByText(/1 правило не проверено: в документе есть указания программам/)).toBeInTheDocument();
    expect(screen.getByText(/2 правила «в порядке» только со слов модели/)).toBeInTheDocument();
    expect(screen.getByText(/Смысловой поиск был недоступен/)).toBeInTheDocument();
  });

  it("серый из-за непроверенного критичного правила — правило названо", () => {
    const data = report({ ok: 26, not_checked: 2 });
    (data as unknown as { rules: unknown[] }).rules = [
      { rule_code: "TRM-01", title: "Односторонний отказ по любым основаниям", outcome: "not_checked" },
    ];
    show("gray", data, { critical_unchecked: ["TRM-01"] });
    expect(screen.getByText(/2 правила не проверено — итог не может быть зелёным/)).toBeInTheDocument();
    details();
    expect(
      screen.getByText(/могут дать «критично»: «Односторонний отказ по любым основаниям»/),
    ).toBeInTheDocument();
  });

  it("сбой модели и непрочитанные документы — списком в предупреждении", () => {
    const data = report({ not_checked: 9, failed: 9, ok: 19 }, [{ severity: "red" }]);
    (data as unknown as { documents: unknown[] }).documents = [
      { id: "d1", filename: "Контракт.docx", status: "failed", problem: "файл повреждён" },
      { id: "d2", filename: "ТЗ.pdf", status: "ready" },
    ];
    show("red", data);
    expect(screen.getByText("1 критичный риск")).toBeInTheDocument();
    expect(screen.getByText("Не все документы удалось обработать")).toBeInTheDocument();
    expect(screen.getByText(/1 файл не открылся, 9 правил не проверено/)).toBeInTheDocument();
    details();
    expect(screen.getByText("«Контракт.docx» — файл повреждён")).toBeInTheDocument();
    expect(screen.getByText(/9 правил не удалось проверить/)).toBeInTheDocument();
  });

  it("причины «не проверено» — своими словами, а не «сбой сервиса»", () => {
    const data = report({ not_checked: 6, failed: 4, ok: 22 });
    (data as unknown as { documents: unknown[] }).documents = [
      { id: "d1", filename: "Контракт.pdf", status: "ready" },
    ];
    show("gray", data, {
      rules_errors: { unverified_violation: 1, unverified_ok: 1, regex_timeout: 1, llm_unavailable: 1 },
      rules_blocked: { partial_search: 2, red_flag: 1 },
      documents_incomplete: ["Контракт.pdf"],
    });
    details();
    expect(screen.getByText(/1 правило не удалось проверить из-за сбоя сервиса/)).toBeInTheDocument();
    expect(screen.getByText(/указала на нарушение по 1 правилу, но её цитата не нашлась/)).toBeInTheDocument();
    expect(screen.getByText(/модель сочла условие нормальным, но цитата не нашлась/)).toBeInTheDocument();
    expect(screen.getByText(/выражение правила работает слишком долго/)).toBeInTheDocument();
    expect(screen.getByText(/2 правила не проверено: документ не поместился целиком/)).toBeInTheDocument();
    expect(screen.getByText(/1 правило не проверено: в тексте есть тревожная формулировка/)).toBeInTheDocument();
    expect(screen.getByText(/«Контракт.pdf» прочитан не полностью/)).toBeInTheDocument();
  });

  it("причины свёрнуты: сразу одна строка из чисел, список — по «Подробнее»", () => {
    show("gray", report({ not_checked: 4, ok: 24 }), {
      rules_blocked: { unreadable: 1, instructions: 1, partial_search: 1, red_flag: 1 },
    });
    const alert = () => within(screen.getByRole("alert"));
    expect(alert().getByText(/4 правила не проверено/)).toBeInTheDocument();
    expect(alert().queryAllByRole("listitem")).toHaveLength(0);
    expect(alert().getByRole("button", { name: "Подробнее" })).toHaveAttribute("aria-expanded", "false");
    details();
    expect(alert().getAllByRole("listitem")).toHaveLength(4);
    fireEvent.click(alert().getByRole("button", { name: "Свернуть" }));
    expect(alert().queryAllByRole("listitem")).toHaveLength(0);
  });

  it("модель не подключена — так и сказано, а не «сбой сервиса, повторите позже»", () => {
    const data = report({ not_checked: 9, failed: 9, ok: 19 }, [{ severity: "red" }]);
    (data as unknown as { rules: unknown[] }).rules = Array.from({ length: 9 }, (_, i) => ({
      rule_code: `R-0${i}`,
      title: "Правило",
      outcome: "not_checked",
      reason: "Нет записанного ответа модели для этого запроса: модель не подключена (демо-режим)",
    }));
    show("red", data); // старая проверка: видов сбоев в counters ещё нет
    details();
    expect(screen.getByText(/9 правил не проверено: модель не подключена/)).toBeInTheDocument();
    expect(screen.queryByText(/сбоя сервиса/)).toBeNull();
  });
});

describe("граница ошибок", () => {
  it("сбой экрана — сообщение вместо белой страницы", () => {
    const Broken = () => {
      throw new Error("сломано");
    };
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      renderApp(
        <ErrorBoundary>
          <Broken />
        </ErrorBoundary>,
      ),
    );
    expect(screen.getByText("Экран не удалось показать")).toBeInTheDocument();
  });
});

import {
  ArrowLeftOutlined,
  DiffOutlined,
  DownloadOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  MailOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Dropdown,
  Grid,
  Popover,
  Progress,
  Space,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { useEditions, useRerun } from "../../api/hooks";
import type { CheckOut, ReportOut } from "../../api/types";
import { TrafficLight } from "../../components/TrafficLight";
import { count, duration, when } from "../../lib/format";
import { downloadClarification, downloadDocx, downloadPdf } from "../../lib/exports";
import { assessedOf, coverageText } from "../../lib/lights";
import { bigNumber, brand, lights, type LightKey } from "../../theme";

const CATEGORY_TITLES: Record<string, string> = {
  terms: "Сроки",
  payment: "Оплата",
  security: "Обеспечение",
  liability: "Ответственность",
  termination: "Расторжение",
  acceptance: "Приёмка",
  requirements: "Требования к участнику",
  subject: "Объект закупки",
  volume: "Объём",
  warranty: "Гарантия",
  national_regime: "Нацрежим",
  consistency: "Согласованность документов",
};

/**
 * Цифра итога. У «критично» и «внимание» — значок светофора: тёмные красный и жёлтый текста
 * (нужны для контраста на белом) почти одной яркости и плохо различаются, значки — цветом и формой.
 */
function Stat({ value, label, light }: { value: number; label: string; light?: LightKey }) {
  const active = Boolean(light && value);
  return (
    <div style={{ minWidth: 76 }}>
      <div
        className="num"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 24,
          fontWeight: 700,
          lineHeight: 1.15,
          color: active && light ? bigNumber[light] : brand.navy,
        }}
      >
        {active && light && <TrafficLight light={light} iconOnly />}
        {/* «1 234»: разряды через неразрывный пробел, число не переносится */}
        <span style={{ whiteSpace: "nowrap" }}>{value.toLocaleString("ru-RU")}</span>
      </div>
      <div className="muted" style={{ fontSize: 13, whiteSpace: "nowrap" }}>
        {label}
      </div>
    </div>
  );
}

function RiskIndex({ report, light }: { report: ReportOut; light: CheckOut["light"] }) {
  const { risk } = report;
  if (risk.index == null) return null;
  const index = Math.round(risk.index);
  // цвет — светофора пакета: своя шкала индекса давала зелёную дугу рядом с красным «критично»
  const color = lights[(light ?? "gray") as LightKey].solid;
  const contributions = Object.entries(risk.contributions).sort((a, b) => b[1] - a[1]);
  const content = (
    <div style={{ width: 300 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Вклад категорий рисков: критичное замечание — полный вес категории, «внимание» — половина.
      </Typography.Paragraph>
      {contributions.length === 0 && <span className="muted">Рисков нет — вклад нулевой</span>}
      {contributions.map(([category, value]) => (
        <div key={category} style={{ marginBottom: 6 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
            <span>{CATEGORY_TITLES[category] ?? category}</span>
            <span className="num">{Math.round(value)}</span>
          </div>
          <Progress percent={Math.min(100, value * 4)} showInfo={false} size="small" strokeColor={brand.slate} />
        </div>
      ))}
    </div>
  );
  return (
    <Popover title="Индекс риска" content={content} trigger={["hover", "click"]} placement="bottomRight">
      <button
        type="button"
        className="plain-button"
        style={{ display: "flex", alignItems: "center", gap: 12 }}
        aria-label={`Индекс риска ${index} из 100, подробнее`}
      >
        <Progress
          type="circle"
          percent={index}
          size={52}
          strokeColor={color}
          format={() => <span className="num" style={{ fontWeight: 700, color: brand.navy }}>{index}</span>}
        />
        <div>
          <div style={{ fontWeight: 600 }}>
            Индекс риска <InfoCircleOutlined style={{ color: brand.gray, fontSize: 12 }} />
          </div>
          <div className="muted" style={{ fontSize: 13, maxWidth: 200 }}>
            {risk.recommendation_text}
          </div>
        </div>
      </button>
    </Popover>
  );
}

export function ReportHeader({
  check,
  report,
  onOpenTab,
}: {
  check: CheckOut;
  report: ReportOut;
  onOpenTab: (tab: string) => void;
}) {
  const { message } = App.useApp();
  const compact = !Grid.useBreakpoint().md;
  const navigate = useNavigate();
  const rerun = useRerun();
  const editions = useEditions(check.id, true);
  const [busy, setBusy] = useState<string | null>(null);
  const [showProblems, setShowProblems] = useState(false);
  const red = report.findings.filter((f) => f.severity === "red").length;
  const yellow = report.findings.filter((f) => f.severity === "yellow").length;
  const summary = report.rules_summary;
  const number = report.card.find((c) => c.key === "procurement_number")?.display;
  const failedDocs = report.documents.filter((d) => d.status === "failed");
  const title = report.package.title;

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    try {
      await action();
    } catch (exc) {
      message.error(exc instanceof ApiError ? exc.message : "Не удалось скачать файл");
    } finally {
      setBusy(null);
    }
  };

  const assessed = assessedOf(summary);
  const coverage = coverageText(assessed, summary.checked);
  const gray = check.light === "gray";
  const verdict =
    red > 0
      ? `${count(red, ["критичный риск", "критичных риска", "критичных рисков"])}${yellow ? ` и ${yellow} — «внимание»` : ""}`
      : yellow > 0
        ? "Критичных рисков нет, есть условия, требующие внимания"
        : gray
          ? "Риски не оценены"
          : "Рисков не найдено";
  // зелёный — утверждение «рисков нет»: при непроверенных правилах оно верно лишь для проверенной части
  const verdictNote = gray
    ? `${coverage} — этого мало, чтобы сказать, что рисков нет`
    : summary.not_checked > 0
      ? `${coverage} — что не проверено и почему`
      : `Все правила проверены (${summary.checked}) — список`;
  // поштучная проверка: «рисков нет» опирается на ответ по каждому пункту, а не на слова модели
  const sweptUnits = (check.counters as { sweep?: { units?: number } }).sweep?.units ?? 0;
  const sweepNote =
    !gray && sweptUnits > 0 ? ` · поштучно проверено ${count(sweptUnits, ["пункт", "пункта", "пунктов"])}` : "";

  const counters = check.counters as {
    rules_blocked?: {
      unreadable?: number;
      instructions?: number;
      partial_search?: number;
      red_flag?: number;
      sweep_disputed?: number;
      sweep_incomplete?: number;
      sweep_failed?: number;
    };
    rules_errors?: Record<string, number>;
    documents_incomplete?: string[];
    vector_search?: string;
    critical_unchecked?: string[];
  };
  const critical = counters.critical_unchecked ?? [];
  const titles = Object.fromEntries(report.rules.map((r) => [r.rule_code, r.title]));
  const blocked = counters.rules_blocked ?? {};
  const errors = counters.rules_errors ?? {};
  // таймаут выражения, неподтверждённая цитата и неподключённая модель — не сбой сервиса:
  // повтор проверки не поможет, у каждой причины своя строка
  // в проверках до разбивки сбоев по видам — по тексту причины правила
  const noModel =
    errors.replay_miss ??
    report.rules.filter((r) => r.outcome === "not_checked" && /модель не подключена/.test(r.reason ?? "")).length;
  const ownReasons =
    (errors.regex_timeout ?? 0) + (errors.unverified_violation ?? 0) + (errors.unverified_ok ?? 0) + noModel;
  const serviceFailed = Math.max(0, summary.failed - ownReasons);
  const failedNames = new Set(failedDocs.map((d) => d.filename));
  const incomplete = (counters.documents_incomplete ?? []).filter((name) => !failedNames.has(name));
  const rulesWord = (n: number) => count(n, ["правило", "правила", "правил"]);
  const problems = [
    // сначала причина (документ не открылся), потом следствие (правила не проверены)
    ...failedDocs.map((d) => `«${d.filename}» — ${d.problem ?? "не удалось открыть"}`),
    ...incomplete.map(
      (name) =>
        `«${name}» прочитан не полностью (страницы не распознаны или файл пропущен) — итог не может быть зелёным`,
    ),
    ...(critical.length
      ? [
          `Не проверены правила, которые могут дать «критично»: ${critical
            .slice(0, 4)
            .map((code) => `«${titles[code] ?? code}»`)
            .join(", ")}${critical.length > 4 ? ` и ещё ${critical.length - 4}` : ""} — поэтому итог не может быть зелёным`,
        ]
      : []),
    ...(noModel > 0
      ? [
          `${rulesWord(noModel)} не проверено: модель не подключена (демонстрационный режим) — смысловые правила проверяются только с подключённой моделью`,
        ]
      : []),
    ...(serviceFailed > 0
      ? [
          `${count(serviceFailed, ["правило не удалось", "правила не удалось", "правил не удалось"])} проверить из-за сбоя сервиса анализа — повторите проверку позже`,
        ]
      : []),
    ...(errors.unverified_violation
      ? [
          `Модель указала на нарушение по ${count(errors.unverified_violation, ["правилу", "правилам", "правилам"])}, но её цитата не нашлась в документе — проверьте вручную`,
        ]
      : []),
    ...(errors.unverified_ok
      ? [
          `${rulesWord(errors.unverified_ok)} не проверено: модель сочла условие нормальным, но цитата не нашлась — проверьте вручную`,
        ]
      : []),
    ...(errors.regex_timeout
      ? [
          `${rulesWord(errors.regex_timeout)} не проверено: выражение правила работает слишком долго — упростите его в редакторе правил`,
        ]
      : []),
    ...(blocked.unreadable
      ? [
          `${rulesWord(blocked.unreadable)} не проверено: текст прочитан не полностью (страницы не распознаны или числа не читаются) — «рисков нет» не доказано`,
        ]
      : []),
    ...(blocked.red_flag
      ? [
          `${rulesWord(blocked.red_flag)} не проверено: в тексте есть тревожная формулировка, а модель сочла условие нормальным — проверьте вручную`,
        ]
      : []),
    ...(blocked.partial_search
      ? [
          `${rulesWord(blocked.partial_search)} не проверено: документ не поместился целиком, а смысловой поиск не работал — «в порядке» по части текста не доказано`,
        ]
      : []),
    ...(blocked.instructions
      ? [
          `${rulesWord(blocked.instructions)} не проверено: в документе есть указания программам проверки — выводы модели по нему не используются`,
        ]
      : []),
    ...(blocked.sweep_disputed
      ? [
          `${rulesWord(blocked.sweep_disputed)} не проверено: поштучная проверка отметила пункт, а проверка правила его не подтвердила — проверьте вручную`,
        ]
      : []),
    ...((blocked.sweep_incomplete ?? 0) + (blocked.sweep_failed ?? 0) > 0
      ? [
          `${rulesWord((blocked.sweep_incomplete ?? 0) + (blocked.sweep_failed ?? 0))} не проверено: поштучная проверка не получила ответа по всем пунктам — «рисков нет» не доказано`,
        ]
      : []),
    ...((summary.unconfirmed ?? 0) > 0
      ? [
          `${rulesWord(summary.unconfirmed ?? 0)} «в порядке» только со слов модели — цитата не подтвердилась, в полноту проверки не входят`,
        ]
      : []),
    ...(counters.vector_search === "unavailable"
      ? ["Смысловой поиск был недоступен — условия, записанные другими словами, могли не найтись"]
      : []),
  ];
  const problemsTitle =
    failedDocs.length && failedDocs.length === report.documents.length
      ? "Ни один документ не удалось обработать"
      : failedDocs.length
        ? "Не все документы удалось обработать"
        : "Проверка неполная";
  // одна строка из чисел — сразу; причины целиком — по «Подробнее», чтобы не заслонять отчёт
  const problemsSummary =
    [
      failedDocs.length
        ? count(failedDocs.length, ["файл не открылся", "файла не открылись", "файлов не открылись"])
        : null,
      summary.not_checked > 0 ? `${rulesWord(summary.not_checked)} не проверено` : null,
    ]
      .filter(Boolean)
      .join(", ") || "Есть оговорки к итогу";
  const notGreen = critical.length > 0 || incomplete.length > 0 ? " — итог не может быть зелёным" : "";

  const actions = (
    <Space wrap size={8}>
      <Dropdown
        menu={{
          items: [
            { key: "docx", icon: <FileTextOutlined />, label: "Отчёт в Word" },
            { key: "pdf", icon: <FileTextOutlined />, label: "Отчёт в PDF" },
          ],
          onClick: ({ key }) =>
            run(key, () => (key === "pdf" ? downloadPdf(check.id, title) : downloadDocx(check.id, title))),
        }}
      >
        <Button type="primary" icon={<DownloadOutlined />} loading={busy === "docx" || busy === "pdf"}>
          Скачать отчёт
        </Button>
      </Dropdown>
      {/* срок и лимит запросов — нормы 44-ФЗ; текст и закон решает бэкенд (clarification_note) */}
      <Tooltip title={report.clarification_note ?? undefined}>
        <Button
          icon={<MailOutlined />}
          loading={busy === "clarification"}
          onClick={() => run("clarification", () => downloadClarification(check.id, title))}
        >
          Запрос разъяснений
          {report.clarification_deadline && ` · до ${report.clarification_deadline.slice(0, 5)}`}
        </Button>
      </Tooltip>
      {/* второстепенные действия — тихие кнопки: на виду, но не спорят с главными */}
      {(editions.data?.length ?? 0) > 0 && (
        <Button
          type="text"
          icon={<DiffOutlined />}
          onClick={() => navigate(`/checks/${check.id}/compare/${editions.data![0].check_id}`)}
        >
          Сравнить с прошлой редакцией
        </Button>
      )}
      <Button
        type="text"
        icon={<ReloadOutlined />}
        loading={rerun.isPending}
        onClick={() =>
          rerun.mutate(check.id, {
            onSuccess: (r) => navigate(`/checks/${r.check_id}`),
            onError: (e) => message.error(e instanceof ApiError ? e.message : "Не удалось"),
          })
        }
      >
        Перепроверить
      </Button>
    </Space>
  );

  return (
    // белая полоса шапки отчёта — отдельным блоком под «На главную», с линиями сверху и снизу
    <div
      style={{
        marginTop: 16,
        background: brand.surface,
        borderTop: `1px solid ${brand.border}`,
        borderBottom: `1px solid ${brand.border}`,
      }}
    >
      <div className="report-band">
        <Link to="/checks" className="link-back">
          <ArrowLeftOutlined /> Все проверки
        </Link>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "12px 24px", alignItems: "center" }}>
          <div style={{ flex: "1 1 360px", minWidth: 0 }}>
            <h1 className="page-title" style={{ marginBottom: 2, overflowWrap: "anywhere" }}>
              {title}
            </h1>
            {/* набор правил и длительность — в подсказке: на виду только то, что нужно сразу */}
            <div
              className="muted"
              style={{ fontSize: 13, display: "flex", flexWrap: "wrap", gap: "2px 14px" }}
              title={[
                check.ruleset && `Набор правил: ${check.ruleset}`,
                check.duration_s != null && `проверка заняла ${duration(check.duration_s)}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            >
              {number && <span className="num">№ {number}</span>}
              {check.law && <span>{check.law}</span>}
              <span>проверено {when(check.finished_at)}</span>
            </div>
          </div>
          {!compact && actions}
        </div>

        {/* итог одной полосой: вывод → цифры → индекс; цвет — из правил, а не из модели */}
        <section className="summary-strip" aria-label="Итог проверки">
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px 16px", flex: "1 1 340px", minWidth: 0 }}>
            <TrafficLight light={check.light} size="lg" />
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <div style={{ fontSize: 16, fontWeight: 600, lineHeight: 1.35 }}>{verdict}</div>
              <button type="button" className="link-quiet" onClick={() => onOpenTab("checked")}>
                {verdictNote}{sweepNote}
              </button>
            </div>
          </div>
          <span className="summary-divider" aria-hidden />
          <div style={{ display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap" }}>
            <Stat value={red} label="критично" light="red" />
            <Stat value={yellow} label="внимание" light="yellow" />
            <Stat value={summary.ok} label="в порядке" light="green" />
            <button
              type="button"
              onClick={() => onOpenTab("checked")}
              className="plain-button"
              aria-label="Показать, что не проверено"
            >
              <Stat
                value={summary.not_checked}
                label="не проверено"
                light="gray"
              />
            </button>
          </div>
          {report.risk.index != null && (
            <>
              <span className="summary-divider" aria-hidden />
              <RiskIndex report={report} light={check.light} />
            </>
          )}
        </section>

        {problems.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 12 }}
            message={problemsTitle}
            description={
              <>
                <span>
                  {problemsSummary}
                  {notGreen}.{" "}
                </span>
                <button
                  type="button"
                  className="link-quiet"
                  aria-expanded={showProblems}
                  aria-controls="problems-details"
                  onClick={() => setShowProblems((v) => !v)}
                >
                  {showProblems ? "Свернуть" : "Подробнее"}
                </button>
                {showProblems && (
                  <ul id="problems-details" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                    {problems.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                )}
              </>
            }
          />
        )}
        {/* на телефоне сначала вывод, потом действия */}
        {compact && <div style={{ marginTop: 12 }}>{actions}</div>}
      </div>
    </div>
  );
}

import { ExperimentOutlined } from "@ant-design/icons";
import { Alert, Button, Input, Select, Space, Typography } from "antd";
import { useState } from "react";

import { api, ApiError } from "../../api/client";
import { TrafficLight } from "../../components/TrafficLight";
import { DOC_TYPES } from "../../lib/catalog";

interface SandboxFinding {
  severity: string;
  kind: string;
  message: string;
  explanation: string | null;
  quote: string | null;
}

interface SandboxResult {
  outcome: "violation" | "ok" | "absent" | "not_checked";
  reason: string | null;
  findings: SandboxFinding[];
  rejected: number;
  facts: { key: string; label: string; display: string; quote: string }[];
}

const OUTCOME_TEXT = {
  violation: "Правило сработало",
  absent: "Условие не найдено — правило сообщит об этом",
  ok: "Правило не сработало: нарушения в тексте нет",
  not_checked: "Не проверено",
};

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

/** «Проверить на тексте»: черновик правила из редактора → задача → результат с цитатой. */
export function RuleSandbox({ getRule }: { getRule: () => Record<string, unknown> | null }) {
  const [text, setText] = useState("");
  const [docType, setDocType] = useState("contract");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SandboxResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    const rule = getRule();
    if (!rule) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const { task_id } = await api<{ task_id: string }>("/api/rules/test", {
        method: "POST",
        json: { rule, text, doc_type: docType },
      });
      for (let i = 0; i < 90; i += 1) {
        const answer = await api<{ status: string; result: SandboxResult | null; error: string | null }>(
          `/api/rules/test/${task_id}`,
        );
        if (answer.status === "done" && answer.result) {
          setResult(answer.result);
          return;
        }
        if (answer.status === "failed") throw new ApiError(0, "sandbox", answer.error ?? "Не удалось");
        await sleep(1000);
      }
      throw new ApiError(0, "timeout", "Проверка на тексте не уложилась в полторы минуты");
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Не удалось проверить правило");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Вставьте фрагмент документа — правило проверит его так же, как пакет. Ничего не сохраняется.
      </Typography.Paragraph>
      <Input.TextArea
        rows={6}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Например: 4.2. Поставка товара осуществляется в выходные и праздничные дни по заявке Заказчика…"
        showCount
        maxLength={30000}
      />
      <Space style={{ marginTop: 8 }} wrap>
        <span className="muted">Это текст из:</span>
        <Select value={docType} onChange={setDocType} options={DOC_TYPES} style={{ minWidth: 240 }} />
        <Button icon={<ExperimentOutlined />} onClick={run} loading={busy} disabled={text.trim().length < 20}>
          Проверить
        </Button>
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      {result && (
        <div style={{ marginTop: 12 }}>
          <Alert
            type={result.outcome === "violation" || result.outcome === "absent" ? "warning" : result.outcome === "ok" ? "success" : "info"}
            showIcon
            message={OUTCOME_TEXT[result.outcome]}
            description={result.reason ?? undefined}
          />
          {result.findings.map((f, i) => (
            <div key={i} style={{ marginTop: 10 }}>
              <Space>
                <TrafficLight light={f.severity} size="sm" />
                <span>{f.message}</span>
              </Space>
              {f.quote && (
                <blockquote className="quote" style={{ marginTop: 6 }}>
                  «{f.quote}»
                </blockquote>
              )}
              {f.explanation && (
                <Typography.Paragraph type="secondary" style={{ fontSize: 13, margin: "4px 0 0" }}>
                  {f.explanation}
                </Typography.Paragraph>
              )}
            </div>
          ))}
          {result.rejected > 0 && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 8 }}>
              Модель назвала цитату, которой нет в тексте, — замечание отклонено проверкой цитат.
            </Typography.Paragraph>
          )}
          {result.facts.length > 0 && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 8 }}>
              Найдено в тексте: {result.facts.map((f) => `${f.label} — ${f.display}`).join("; ")}
            </Typography.Paragraph>
          )}
        </div>
      )}
    </div>
  );
}

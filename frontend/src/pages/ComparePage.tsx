import { DownloadOutlined } from "@ant-design/icons";
import { App, Breadcrumb, Button, Card, Collapse, Empty, Result, Skeleton, Space, Table, Tag } from "antd";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { useCheck, useCompare } from "../api/hooks";
import type { DiffPart, UnitChange } from "../api/types";
import { TrafficLight } from "../components/TrafficLight";
import { when } from "../lib/format";
import { downloadComparison } from "../lib/exports";
import { brand, lights } from "../theme";

const STATUS: Record<UnitChange["status"], { label: string; color: string }> = {
  changed: { label: "Изменён", color: "processing" },
  renumbered: { label: "Перенумерован", color: "default" },
  added: { label: "Добавлен", color: "success" },
  removed: { label: "Удалён", color: "error" },
};

/** Изменения по словам: удалённое — зачёркнуто красным, вставленное — выделено зелёным. */
function Diff({ parts }: { parts: DiffPart[] }) {
  return (
    <p style={{ margin: 0, lineHeight: 1.7 }}>
      {parts.map((part, i) =>
        part.op === "equal" ? (
          <span key={i}>{part.text} </span>
        ) : part.op === "delete" ? (
          <del key={i} style={{ color: lights.red.text, background: lights.red.bg, textDecorationThickness: 2 }}>
            {part.text}
          </del>
        ) : (
          <ins key={i} style={{ color: lights.green.text, background: lights.green.bg, textDecoration: "none", fontWeight: 600 }}>
            {` ${part.text} `}
          </ins>
        ),
      )}
    </p>
  );
}

function Change({ change }: { change: UnitChange }) {
  const unit = change.new ?? change.old;
  const number =
    change.status === "renumbered" ? `${change.old?.number} → ${change.new?.number}` : unit?.number ?? "абзац";
  return (
    <div style={{ padding: "12px 0", borderTop: `1px solid ${brand.border}` }}>
      <Space size={8} style={{ marginBottom: 6 }}>
        <Tag color={STATUS[change.status].color} bordered={false}>
          {STATUS[change.status].label}
        </Tag>
        <span className="num" style={{ fontWeight: 600 }}>
          {number}
        </span>
        <span className="muted" style={{ fontSize: 12 }}>
          стр. {unit?.page}
        </span>
      </Space>
      {change.status === "changed" ? (
        <Diff parts={change.diff} />
      ) : change.status === "removed" ? (
        <p style={{ margin: 0, color: lights.red.text }}>
          <del>{change.old?.text}</del>
        </p>
      ) : (
        <p style={{ margin: 0 }}>{unit?.text}</p>
      )}
    </div>
  );
}

export function ComparePage() {
  const { id = "", other = "" } = useParams();
  const { message } = App.useApp();
  const comparison = useCompare(id, other);
  const current = useCheck(id);
  const previous = useCheck(other);
  const [busy, setBusy] = useState(false);

  if (comparison.isLoading) {
    return (
      <div className="page">
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }
  if (comparison.error || !comparison.data) {
    return (
      <div className="page">
        <Result
          status="error"
          title="Не удалось сравнить редакции"
          subTitle={comparison.error instanceof ApiError ? comparison.error.message : undefined}
        />
      </div>
    );
  }
  const data = comparison.data;
  const title = current.data?.package.title ?? "Закупка";
  const risks = [
    ...data.risks.appeared.map((r) => ({ ...r, what: "Появился" })),
    ...data.risks.severity_changed.map((r) => ({ ...r, what: "Изменился цвет" })),
    ...data.risks.resolved.map((r) => ({ ...r, what: "Ушёл" })),
  ];
  const changedDocs = data.documents.filter((d) => d.status !== "same");

  return (
    <div className="page">
      <Breadcrumb
        style={{ marginBottom: 8 }}
        items={[
          { title: <Link to="/checks">Проверки</Link> },
          { title: <Link to={`/checks/${id}`}>{title}</Link> },
          { title: "Что изменилось" },
        ]}
      />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div>
          <h1 className="page-title">Что изменилось в документации</h1>
          <p className="page-subtitle">
            Было: {previous.data?.package.title} ({when(previous.data?.created_at)}) · стало: {title} (
            {when(current.data?.created_at)})
          </p>
        </div>
        <Button
          icon={<DownloadOutlined />}
          loading={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await downloadComparison(id, other, title);
            } catch (exc) {
              message.error(exc instanceof ApiError ? exc.message : "Не удалось скачать");
            } finally {
              setBusy(false);
            }
          }}
        >
          Скачать в Word
        </Button>
      </div>

      <Card title="Риски" size="small" style={{ marginBottom: 16 }}>
        {risks.length === 0 ? (
          <span className="muted">Набор рисков не изменился.</span>
        ) : (
          <Table
            size="small"
            pagination={false}
            rowKey={(r) => `${r.what}-${r.rule_code}`}
            dataSource={risks}
            columns={[
              { title: "Что", dataIndex: "what", width: 150 },
              {
                title: "Цвет",
                width: 260,
                render: (_, r) => (
                  <Space size={6}>
                    {"old_severity" in r && <TrafficLight light={r.old_severity as string} size="sm" />}
                    {"old_severity" in r && <span className="muted">→</span>}
                    <TrafficLight light={r.what === "Ушёл" ? "green" : r.severity} size="sm" label={r.what === "Ушёл" ? "Устранён" : undefined} />
                  </Space>
                ),
              },
              { title: "Риск", render: (_, r) => `${r.rule_code}. ${r.title}` },
            ]}
          />
        )}
      </Card>

      <Card title="Условия закупки" size="small" style={{ marginBottom: 16 }}>
        {data.card.length === 0 ? (
          <span className="muted">Условия в карточке закупки не изменились.</span>
        ) : (
          <Table
            size="small"
            pagination={false}
            rowKey="key"
            dataSource={data.card}
            columns={[
              { title: "Условие", dataIndex: "label", width: 260 },
              { title: "Было", dataIndex: "old", render: (v: string | null) => <span className="num">{v ?? "—"}</span> },
              { title: "Стало", dataIndex: "new", render: (v: string | null) => <strong className="num">{v ?? "—"}</strong> },
            ]}
          />
        )}
      </Card>

      <Card title="Пункты документов" size="small">
        {changedDocs.length === 0 ? (
          <Empty description="Текст документов не изменился" />
        ) : (
          <Collapse
            ghost
            defaultActiveKey={changedDocs.map((_, i) => String(i))}
            items={changedDocs.map((doc, i) => ({
              key: String(i),
              label: (
                <Space>
                  <strong>{doc.new_filename ?? doc.old_filename}</strong>
                  <span className="muted">
                    {doc.status === "added"
                      ? "новый документ"
                      : doc.status === "removed"
                        ? "документ удалён"
                        : `изменений: ${doc.changes.length}`}
                  </span>
                </Space>
              ),
              children: doc.changes.map((change, j) => <Change key={j} change={change} />),
            }))}
          />
        )}
      </Card>
    </div>
  );
}

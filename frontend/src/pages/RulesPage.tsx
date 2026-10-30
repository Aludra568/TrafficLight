import { DownloadOutlined, PlusOutlined, SearchOutlined } from "@ant-design/icons";
import { App, Button, Card, Input, Select, Space, Table, Tag, Tooltip } from "antd";
import { useMemo, useState } from "react";

import { ApiError, download } from "../api/client";
import { useRules } from "../api/hooks";
import type { RuleListItem } from "../api/types";
import { TrafficLight } from "../components/TrafficLight";
import { RULE_TYPES } from "../lib/catalog";
import { when } from "../lib/format";
import { RuleEditor } from "./rules/RuleEditor";

export function RulesPage() {
  const { message } = App.useApp();
  const rules = useRules();
  const [search, setSearch] = useState("");
  const [type, setType] = useState<string | undefined>();
  const [editing, setEditing] = useState<{ code: string | null } | null>(null);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (rules.data ?? []).filter(
      (r) =>
        (!type || r.type === type) &&
        (!q || r.code.toLowerCase().includes(q) || r.title.toLowerCase().includes(q)),
    );
  }, [rules.data, search, type]);

  return (
    <div className="page">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div>
          <h1 className="page-title">Правила</h1>
          <p className="page-subtitle">Изменения действуют со следующей проверки.</p>
        </div>
        <Space>
          <Button
            icon={<DownloadOutlined />}
            onClick={() =>
              download("/api/rulesets/44fz-supplier-base/export", "44fz_supplier_base.yaml").catch((e) =>
                message.error(e instanceof ApiError ? e.message : "Не удалось"),
              )
            }
          >
            Набор в YAML
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ code: null })}>
            Новое правило
          </Button>
        </Space>
      </div>
      <Card styles={{ body: { padding: 0 } }}>
        <Space style={{ padding: 16 }} wrap>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="Код или название"
            aria-label="Поиск правила по коду или названию"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 280 }}
          />
          <Select
            allowClear
            placeholder="Как проверяется"
            style={{ minWidth: 240 }}
            value={type}
            onChange={setType}
            options={Object.entries(RULE_TYPES).map(([value, label]) => ({ value, label }))}
          />
        </Space>
        <Table<RuleListItem>
          rowKey="code"
          size="middle"
          loading={rules.isLoading}
          dataSource={shown}
          pagination={false}
          // на телефоне таблица прокручивается внутри карточки, а не вся страница
          scroll={{ x: "max-content" }}
          onRow={(row) => ({ onClick: () => setEditing({ code: row.code }), style: { cursor: "pointer" } })}
          columns={[
            { title: "Код", dataIndex: "code", width: 96, render: (v: string) => <span className="num">{v}</span> },
            {
              title: "Правило",
              dataIndex: "title",
              render: (title: string, row) => (
                <div>
                  <div style={{ fontWeight: 500 }}>{title}</div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {row.category_title} · {RULE_TYPES[row.type] ?? row.type}
                  </div>
                </div>
              ),
            },
            {
              title: "Цвет",
              dataIndex: "severity",
              width: 140,
              render: (v: string | null) => (v ? <TrafficLight light={v} size="sm" /> : "—"),
            },
            {
              title: "Статус",
              width: 190,
              render: (_, row) => (
                <Space size={4} wrap>
                  {row.status === "verified" ? (
                    <Tooltip title={row.verified_by ?? undefined}>
                      <Tag color="success" bordered={false}>
                        проверено юристом
                      </Tag>
                    </Tooltip>
                  ) : (
                    <Tag bordered={false}>черновик</Tag>
                  )}
                  {row.stale && (
                    <Tooltip title={`Норма сверялась ${row.edition_checked ?? "давно"} — пора перепроверить`}>
                      <Tag color="warning" bordered={false}>
                        сверить норму
                      </Tag>
                    </Tooltip>
                  )}
                  {!row.enabled && <Tag bordered={false}>выключено</Tag>}
                </Space>
              ),
            },
            {
              title: "За 30 дней",
              dataIndex: "triggers_30d",
              width: 110,
              align: "right",
              render: (v: number) => <span className="num">{v}</span>,
            },
            {
              title: "Точность",
              width: 110,
              align: "right",
              render: (_, row) =>
                row.precision == null ? (
                  <span className="muted">—</span>
                ) : (
                  <Tooltip title={`По ${row.feedback_count} отметкам «верно / ошибка»`}>
                    <span className="num">{Math.round(row.precision * 100)} %</span>
                  </Tooltip>
                ),
            },
            {
              title: "Версия",
              width: 130,
              render: (_, row) => (
                <span className="muted" style={{ fontSize: 12 }}>
                  v{row.version} · {when(row.updated_at)}
                </span>
              ),
            },
          ]}
        />
      </Card>
      <RuleEditor code={editing?.code ?? null} open={editing !== null} onClose={() => setEditing(null)} />
    </div>
  );
}

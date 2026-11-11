import { ArrowRightOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Card, Collapse, Empty, Input, List, Select, Space, Tag } from "antd";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { PortfolioFinding, RepeatedRisk } from "../../api/portfolios";
import { TrafficLight } from "../../components/TrafficLight";
import { count } from "../../lib/format";
import { brand } from "../../theme";

const STATUS_LABELS: Record<string, string> = {
  new: "Не обработано",
  in_progress: "Принято к работе",
  resolved: "Отработано",
  not_applicable: "Неактуально",
};

export function PortfolioRisksAndFindings({
  repeatedRisks,
  findings,
}: {
  repeatedRisks: RepeatedRisk[];
  findings: PortfolioFinding[];
}) {
  const [search, setSearch] = useState("");
  const [severityFilter, setSeverityFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const categories = useMemo(() => {
    const set = new Set<string>();
    findings.forEach((f) => {
      if (f.category) set.add(f.category);
    });
    return Array.from(set);
  }, [findings]);

  const filteredFindings = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = findings.filter((f) => {
      if (severityFilter !== "all" && f.severity !== severityFilter) return false;
      if (categoryFilter !== "all" && f.category !== categoryFilter) return false;
      if (statusFilter !== "all" && (f.handling_status ?? "new") !== statusFilter) return false;
      if (!q) return true;
      return (
        f.title.toLowerCase().includes(q) ||
        f.rule_code.toLowerCase().includes(q) ||
        f.source_name.toLowerCase().includes(q) ||
        (f.quote && f.quote.toLowerCase().includes(q))
      );
    });

    return [...list].sort((a, b) => {
      const rank = (s: string) => (s === "red" ? 0 : 1);
      return rank(a.severity) - rank(b.severity);
    });
  }, [findings, search, severityFilter, categoryFilter, statusFilter]);

  return (
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      {repeatedRisks.length > 0 && (
        <Card title="Повторяющиеся риски в портфеле" size="small">
          <List
            size="small"
            dataSource={repeatedRisks}
            renderItem={(item) => (
              <List.Item>
                <div style={{ display: "flex", justifyContent: "space-between", width: "100%", alignItems: "center" }}>
                  <div>
                    <span style={{ fontWeight: 600 }}>{item.title}</span>
                    <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                      {item.rule_code}
                    </span>
                  </div>
                  <Tag color="red">{count(item.procurements, ["закупка", "закупки", "закупок"])}</Tag>
                </div>
              </List.Item>
            )}
          />
        </Card>
      )}

      <Card title={`Замечания портфеля (${filteredFindings.length})`} size="small">
        <Space wrap style={{ marginBottom: 14 }}>
          <Input
            placeholder="Поиск по замечанию, правилу или цитате"
            prefix={<SearchOutlined />}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
            style={{ width: 260 }}
          />
          <Select
            value={severityFilter}
            onChange={setSeverityFilter}
            style={{ width: 130 }}
            options={[
              { value: "all", label: "Все риски" },
              { value: "red", label: "Критично" },
              { value: "yellow", label: "Внимание" },
            ]}
          />
          {categories.length > 0 && (
            <Select
              value={categoryFilter}
              onChange={setCategoryFilter}
              style={{ width: 150 }}
              aria-label="Категория замечания"
              options={[{ value: "all", label: "Все категории" }, ...categories.map((c) => ({
                value: c, label: findings.find((f) => f.category === c)?.category_title || c,
              }))]}
            />
          )}
          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            style={{ width: 160 }}
            options={[
              { value: "all", label: "Все статусы" },
              { value: "new", label: "Не обработано" },
              { value: "in_progress", label: "Принято к работе" },
              { value: "resolved", label: "Отработано" },
              { value: "not_applicable", label: "Неактуально" },
            ]}
          />
        </Space>

        {filteredFindings.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Замечаний по выбранным фильтрам не найдено" />
        ) : (
          <List
            dataSource={filteredFindings}
            pagination={{ pageSize: 10, showSizeChanger: false }}
            renderItem={(f) => {
              const checkUrl = `/checks/${f.check_id}?finding=${f.id}${f.document_id ? `&document=${f.document_id}` : ""}${f.page != null ? `&page=${f.page}` : ""}`;
              return (
                <List.Item style={{ display: "block", padding: "12px 14px", border: `1px solid ${brand.border}`, borderRadius: 8, marginBottom: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
                    <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                      <TrafficLight light={f.severity} iconOnly style={{ marginTop: 2 }} />
                      <div>
                        <div style={{ fontWeight: 600, fontSize: 14 }}>{f.title}</div>
                        <Space size={8} wrap style={{ marginTop: 4 }}>
                          <span className="muted" style={{ fontSize: 12 }}>{f.rule_code}</span>
                          <Tag>{f.source_name}</Tag>
                          {f.category && <Tag bordered={false}>{f.category}</Tag>}
                          <Tag color="blue">{STATUS_LABELS[f.handling_status ?? "new"] ?? f.handling_status}</Tag>
                        </Space>
                      </div>
                    </div>
                    <Button type="link" size="small" icon={<ArrowRightOutlined />}>
                      <Link to={checkUrl}>В отчёт</Link>
                    </Button>
                  </div>
                  {f.quote && (
                    <div style={{ marginTop: 8 }}>
                      <Collapse
                        ghost
                        size="small"
                        items={[
                          {
                            key: "quote",
                            label: <span className="muted" style={{ fontSize: 12 }}>Цитата из документа</span>,
                            children: <blockquote className="quote">{f.quote}</blockquote>,
                          },
                        ]}
                      />
                    </div>
                  )}
                </List.Item>
              );
            }}
          />
        )}
      </Card>
    </Space>
  );
}

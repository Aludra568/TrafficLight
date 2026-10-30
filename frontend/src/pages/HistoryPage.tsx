import { SearchOutlined } from "@ant-design/icons";
import { Card, Input, Segmented, Table } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useChecks } from "../api/hooks";
import type { CheckListItem } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { StatusTag } from "../components/StatusTag";
import { TrafficLight } from "../components/TrafficLight";
import { when } from "../lib/format";
import { useDebounced } from "../lib/useDebounced";

export function HistoryPage() {
  const navigate = useNavigate();
  // специалист видит только свои проверки, методолог и администратор — все
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [light, setLight] = useState<string>("all");
  const [search, setSearch] = useState("");
  const deferred = useDebounced(search);
  const checks = useChecks({
    page,
    pageSize: 20,
    light: light === "all" ? undefined : light,
    search: deferred,
  });
  const filtered = light !== "all" || deferred.trim() !== "";

  return (
    <div className="page">
      <h1 className="page-title">Проверки</h1>
      <p className="page-subtitle">
        {can("methodologist") ? "Все проверки" : "Ваши проверки"}, новые сверху.
      </p>
      <Card styles={{ body: { padding: 0 } }}>
        <div style={{ padding: 16, display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
          {/* пять фильтров не помещаются в телефон — прокручиваются внутри, а не вся страница */}
          <div className="scroll-x">
            <Segmented
              value={light}
              onChange={(v) => {
                setLight(String(v));
                setPage(1);
              }}
              options={[
                { value: "all", label: "Все" },
                { value: "red", label: "Критично" },
                { value: "yellow", label: "Внимание" },
                { value: "green", label: "Нет рисков" },
                { value: "gray", label: "Не оценено" },
              ]}
            />
          </div>
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: "#9A9FAB" }} />}
            placeholder="Название пакета или файла"
            aria-label="Поиск по названию пакета или файла"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            style={{ flex: "1 1 220px", maxWidth: 360 }}
          />
        </div>
        <Table<CheckListItem>
          rowKey="id"
          loading={checks.isLoading}
          dataSource={checks.data?.items ?? []}
          scroll={{ x: 720 }}
          locale={{
            emptyText: filtered ? (
              "Ничего не нашлось — измените поиск или фильтр"
            ) : (
              <span>
                Проверок пока нет. <Link to="/new">Начать первую</Link>
              </span>
            ),
          }}
          onRow={(row) => ({ onClick: () => navigate(`/checks/${row.id}`), style: { cursor: "pointer" } })}
          pagination={{
            current: page,
            pageSize: 20,
            total: checks.data?.total ?? 0,
            onChange: setPage,
            showSizeChanger: false,
            hideOnSinglePage: true,
          }}
          columns={[
            {
              title: "Итог",
              width: 170,
              render: (_, row) =>
                row.light ? <TrafficLight light={row.light} size="sm" /> : <StatusTag status={row.status} />,
            },
            {
              title: "Пакет",
              dataIndex: "package_title",
              render: (title: string, row) => <Link to={`/checks/${row.id}`}>{title}</Link>,
            },
            {
              title: "Замечаний",
              dataIndex: "findings",
              width: 120,
              align: "right",
              render: (v: number) => <span className="num">{v}</span>,
            },
            {
              title: "Индекс риска",
              dataIndex: "risk_index",
              width: 130,
              align: "right",
              render: (v: number | null) => <span className="num">{v == null ? "—" : Math.round(v)}</span>,
            },
            {
              title: "Проверено",
              dataIndex: "created_at",
              width: 170,
              render: (v: string) => <span className="muted">{when(v)}</span>,
            },
          ]}
        />
      </Card>
    </div>
  );
}

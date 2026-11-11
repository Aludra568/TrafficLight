import { ArrowRightOutlined } from "@ant-design/icons";
import { Progress, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";

import type { PortfolioItemOut } from "../../api/portfolios";
import { TrafficLight } from "../../components/TrafficLight";

export function PortfolioItems({ items }: { items: PortfolioItemOut[] }) {
  const columns: ColumnsType<PortfolioItemOut> = [
    {
      title: "№",
      dataIndex: "position",
      key: "position",
      width: 50,
      render: (pos: number) => <span className="num muted">{pos + 1}</span>,
    },
    {
      title: "Закупка / Документ",
      dataIndex: "source_name",
      key: "source_name",
      ellipsis: true,
      render: (name: string, record) => (
        <div>
          <span style={{ fontWeight: 600 }}>{name}</span>
          {record.error && (
            <div style={{ color: "#dc2626", fontSize: 12, marginTop: 2 }}>{record.error}</div>
          )}
        </div>
      ),
    },
    {
      title: "Статус",
      key: "status",
      width: 140,
      render: (_, record) => {
        if (record.status === "done") {
          return <Tag color="success">Готово</Tag>;
        }
        if (record.status === "running") {
          return (
            <div style={{ width: 100 }}>
              <Progress percent={record.progress} size="small" status="active" />
            </div>
          );
        }
        if (record.status === "failed") {
          return (
            <Tooltip title={record.error ?? "Ошибка проверки"}>
              <Tag color="error">Сбой</Tag>
            </Tooltip>
          );
        }
        return <Tag color="default">В очереди</Tag>;
      },
    },
    {
      title: "Светофор",
      dataIndex: "light",
      key: "light",
      width: 130,
      render: (light) => (light ? <TrafficLight light={light} size="sm" /> : <span className="muted">—</span>),
    },
    {
      title: "Замечания",
      dataIndex: "findings",
      key: "findings",
      width: 100,
      align: "center",
      render: (findings: number) => <span className="num">{findings}</span>,
    },
    {
      title: "",
      key: "action",
      width: 90,
      render: (_, record) =>
        record.check_id ? (
          <Link to={`/checks/${record.check_id}`}>Отчёт <ArrowRightOutlined /></Link>
        ) : (
          <span className="muted">—</span>
        ),
    },
  ];

  return (
    <Table
      rowKey="id"
      columns={columns}
      dataSource={items}
      pagination={false}
      size="middle"
      scroll={{ x: 620 }}
    />
  );
}

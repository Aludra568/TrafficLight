import { ClockCircleOutlined, InfoCircleOutlined } from "@ant-design/icons";
import { Badge, Card, Space, Tag, Tooltip, Typography } from "antd";

import type { PortfolioOut } from "../../api/portfolios";
import { TrafficLight } from "../../components/TrafficLight";
import { brand, lights } from "../../theme";

export function PortfolioSummary({ portfolio }: { portfolio: PortfolioOut }) {
  const dist = portfolio.distribution ?? {};
  const notReady = portfolio.running + portfolio.waiting;

  return (
    <Card styles={{ body: { padding: "16px 20px" } }} style={{ marginBottom: 16 }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <Space size={12} align="center" wrap>
            <Typography.Text strong style={{ fontSize: 16 }}>
              Общий статус:
            </Typography.Text>
            <TrafficLight light={portfolio.light} size="lg" />
            {!portfolio.complete && (
              <Tooltip title="Проверка части закупок ещё выполняется. Итог может измениться">
                <Tag color="warning" icon={<ClockCircleOutlined />}>
                  Предварительно
                </Tag>
              </Tooltip>
            )}
          </Space>

          <Space size={16} wrap>
            <div style={{ textAlign: "center", minWidth: 60 }}>
              <div style={{ fontSize: 20, fontWeight: 700 }} className="num">{portfolio.total}</div>
              <div className="muted" style={{ fontSize: 12 }}>Всего</div>
            </div>
            <div style={{ textAlign: "center", minWidth: 60 }}>
              <div style={{ fontSize: 20, fontWeight: 700, color: brand.navy }} className="num">{portfolio.done}</div>
              <div className="muted" style={{ fontSize: 12 }}>Готово</div>
            </div>
            {portfolio.running > 0 && (
              <div style={{ textAlign: "center", minWidth: 60 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: brand.slate }} className="num">{portfolio.running}</div>
                <div className="muted" style={{ fontSize: 12 }}>В работе</div>
              </div>
            )}
            {portfolio.waiting > 0 && (
              <div style={{ textAlign: "center", minWidth: 60 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: lights.gray.text }} className="num">{portfolio.waiting}</div>
                <div className="muted" style={{ fontSize: 12 }}>Ожидает</div>
              </div>
            )}
            {portfolio.failed > 0 && (
              <div style={{ textAlign: "center", minWidth: 60 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: lights.red.text }} className="num">{portfolio.failed}</div>
                <div className="muted" style={{ fontSize: 12 }}>С ошибкой</div>
              </div>
            )}
          </Space>
        </div>

        <div style={{ borderTop: `1px solid ${brand.border}`, paddingTop: 12 }}>
          <Space size={8} wrap align="center">
            <Typography.Text type="secondary" style={{ fontSize: 13, marginRight: 4 }}>
              Распределение рисков:
            </Typography.Text>
            <Badge
              count={dist.red ?? 0}
              showZero
              style={{ backgroundColor: lights.red.solid }}
              title="Критичные риски"
            >
              <Tag color="error">Критично</Tag>
            </Badge>
            <Badge
              count={dist.yellow ?? 0}
              showZero
              style={{ backgroundColor: lights.yellow.solid }}
              title="Внимание"
            >
              <Tag color="warning">Внимание</Tag>
            </Badge>
            <Badge
              count={dist.green ?? 0}
              showZero
              style={{ backgroundColor: lights.green.solid }}
              title="Без рисков"
            >
              <Tag color="success">Без рисков</Tag>
            </Badge>
            <Badge
              count={dist.gray ?? 0}
              showZero
              style={{ backgroundColor: lights.gray.solid }}
              title="Не оценено"
            >
              <Tag>Не оценено</Tag>
            </Badge>
            {notReady > 0 && (
              <Tooltip title="Закупки, которые ещё проверяются или ожидают в очереди">
                <Tag icon={<InfoCircleOutlined />} color="default">
                  В обработке: {notReady}
                </Tag>
              </Tooltip>
            )}
          </Space>
        </div>
      </Space>
    </Card>
  );
}

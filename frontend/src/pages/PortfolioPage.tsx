import { HistoryOutlined, PlusOutlined } from "@ant-design/icons";
import { Tabs } from "antd";
import { useParams } from "react-router-dom";

import { PortfolioCreate } from "./portfolio/PortfolioCreate";
import { PortfolioHistory } from "./portfolio/PortfolioHistory";
import { PortfolioView } from "./portfolio/PortfolioView";

export function PortfolioPage() {
  const { id } = useParams<{ id: string }>();

  if (id) {
    return <PortfolioView id={id} />;
  }

  return (
    <div className="page">
      <h1 className="page-title">Портфель закупок</h1>
      <p className="page-subtitle">
        Пакетная проверка нескольких закупок, сводный анализ рисков и экспорт результатов.
      </p>

      <Tabs
        defaultActiveKey="create"
        items={[
          {
            key: "create",
            label: (
              <span>
                <PlusOutlined /> Новый портфель
              </span>
            ),
            children: <PortfolioCreate />,
          },
          {
            key: "history",
            label: (
              <span>
                <HistoryOutlined /> История портфелей
              </span>
            ),
            children: <PortfolioHistory />,
          },
        ]}
      />
    </div>
  );
}

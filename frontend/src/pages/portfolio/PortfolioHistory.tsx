import { ArrowRightOutlined, FolderOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Empty, List, Skeleton, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { usePortfolios } from "../../api/portfolios";
import { count, when } from "../../lib/format";
import { brand } from "../../theme";

export function PortfolioHistory() {
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const portfoliosQuery = usePortfolios(page, pageSize);

  const data = portfoliosQuery.data;
  const total = data?.total ?? 0;
  const items = data?.items ?? [];

  if (portfoliosQuery.isLoading && !data) {
    return (
      <Card style={{ maxWidth: 880, margin: "0 auto" }}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </Card>
    );
  }

  if (portfoliosQuery.isError) {
    return <Alert type="error" showIcon message="Не удалось загрузить историю портфелей"
      action={<Button onClick={() => void portfoliosQuery.refetch()}>Повторить</Button>} />;
  }

  if (items.length === 0) {
    return (
      <Card style={{ maxWidth: 880, margin: "0 auto" }}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="Портфелей пока нет. Создайте первый портфель на соседней вкладке."
        />
      </Card>
    );
  }

  return (
    <Card styles={{ body: { padding: 16 } }} style={{ maxWidth: 880, margin: "0 auto" }}>
      <List
        dataSource={items}
        pagination={{
          current: page,
          pageSize,
          total,
          onChange: setPage,
          showSizeChanger: false,
        }}
        renderItem={(item) => (
          <List.Item
            actions={[
              <Button type="link" key="open" icon={<ArrowRightOutlined />}>
                <Link to={`/portfolios/${item.id}`}>Открыть</Link>
              </Button>,
            ]}
          >
            <List.Item.Meta
              avatar={<FolderOutlined style={{ fontSize: 24, color: brand.slate, marginTop: 4 }} />}
              title={
                <Link to={`/portfolios/${item.id}`} style={{ fontWeight: 600, fontSize: 16 }}>
                  {item.title}
                </Link>
              }
              description={
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  Создан {when(item.created_at)} · {count(item.total, ["закупка", "закупки", "закупок"])}
                </Typography.Text>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  );
}

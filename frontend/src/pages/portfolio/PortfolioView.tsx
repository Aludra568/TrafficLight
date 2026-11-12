import { ArrowLeftOutlined, FileExcelOutlined, FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Skeleton, Space, Tabs } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../../api/client";
import {
  downloadPortfolioDocx,
  downloadPortfolioExcel,
  usePortfolio,
  useRetryPortfolio,
} from "../../api/portfolios";
import { when } from "../../lib/format";
import { PortfolioItems } from "./PortfolioItems";
import { PortfolioDelete } from "./PortfolioDelete";
import { PortfolioRisksAndFindings } from "./PortfolioRisksAndFindings";
import { PortfolioSummary } from "./PortfolioSummary";
import { PortfolioEditTitle } from "./PortfolioEditTitle";
import { PortfolioEditAddItems } from "./PortfolioEditAddItems";
import { PortfolioEditDocuments } from "./PortfolioEditDocuments";
import type { PortfolioItemOut } from "../../api/portfolios";

export function PortfolioView({ id }: { id: string }) {
  const { message } = App.useApp();
  const portfolioQuery = usePortfolio(id);
  const retryMutation = useRetryPortfolio(id);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [documentsItem, setDocumentsItem] = useState<PortfolioItemOut | null>(null);

  const portfolio = portfolioQuery.data;

  if (portfolioQuery.isLoading && !portfolio) {
    return (
      <div className="page">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  }

  if (portfolioQuery.isError || !portfolio) {
    return (
      <div className="page">
        <Alert
          type="error"
          message="Не удалось загрузить портфель"
          description="Портфель не найден или произошла ошибка связи с сервером."
          action={
            <Button type="primary">
              <Link to="/portfolios">К списку портфелей</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const canRetry = portfolio.failed > 0 || (portfolio.waiting > 0 && portfolio.running === 0);

  const handleRetry = () => {
    retryMutation.mutate(undefined, {
      onSuccess: (res) => {
        if (res.dispatch_error) {
          message.warning(`Перезапуск с предупреждением: ${res.dispatch_error}`);
        } else {
          message.success("Неуспешные закупки перезапущены");
        }
      },
      onError: (err) => {
        message.error(err instanceof ApiError ? err.message : "Не удалось перезапустить проверки");
      },
    });
  };

  const handleDownload = async (type: "xlsx" | "docx") => {
    setDownloading(type);
    try {
      if (type === "xlsx") {
        await downloadPortfolioExcel(portfolio.id, portfolio.title);
      } else {
        await downloadPortfolioDocx(portfolio.id, portfolio.title);
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : "Не удалось скачать выгрузку");
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="page">
      <div style={{ marginBottom: 16 }}>
        <Link to="/portfolios" className="link-back" style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <ArrowLeftOutlined /> Все портфели
        </Link>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <div>
            <PortfolioEditTitle portfolioId={id} title={portfolio.title}
              version={portfolio.title_version} canEdit={portfolio.can_edit} />
            <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
              Создан {when(portfolio.created_at)}
            </div>
          </div>

          <Space wrap>
            {portfolio.can_edit && <Button type="primary" onClick={() => setAdding(true)}>Добавить закупки</Button>}
            <PortfolioDelete id={portfolio.id} disabled={portfolio.running > 0} />
            {canRetry && (
              <Button
                icon={<ReloadOutlined />}
                loading={retryMutation.isPending}
                onClick={handleRetry}
              >
                Повторить неуспешные
              </Button>
            )}
            <Button
              icon={<FileExcelOutlined />}
              loading={downloading === "xlsx"}
              onClick={() => handleDownload("xlsx")}
            >
              Excel
            </Button>
            <Button
              icon={<FileTextOutlined />}
              loading={downloading === "docx"}
              onClick={() => handleDownload("docx")}
            >
              Word
            </Button>
          </Space>
        </div>
      </div>

      <PortfolioSummary portfolio={portfolio} />

      <Card styles={{ body: { padding: "12px 16px" } }}>
        <Tabs
          defaultActiveKey="items"
          items={[
            {
              key: "items",
              label: `Закупки (${portfolio.items.length})`,
              children: <PortfolioItems items={portfolio.items} portfolioId={id}
                canEdit={portfolio.can_edit} onDocuments={setDocumentsItem} />,
            },
            {
              key: "findings",
              label: `Риски и замечания (${portfolio.findings.length})`,
              children: (
                <PortfolioRisksAndFindings
                  repeatedRisks={portfolio.repeated_risks}
                  findings={portfolio.findings}
                />
              ),
            },
          ]}
        />
      </Card>
      {adding && <PortfolioEditAddItems portfolioId={id} open onClose={() => setAdding(false)} />}
      {documentsItem && <PortfolioEditDocuments key={documentsItem.id} portfolioId={id}
        item={portfolio.items.find((item) => item.id === documentsItem.id) ?? documentsItem}
        open onClose={() => setDocumentsItem(null)} />}
    </div>
  );
}

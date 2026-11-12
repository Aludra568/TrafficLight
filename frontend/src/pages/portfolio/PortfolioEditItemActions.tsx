import { App, Button, Popconfirm, Space } from "antd";

import { ApiError } from "../../api/client";
import { useRemovePortfolioItem } from "../../api/portfolioEditing";
import type { PortfolioItemOut } from "../../api/portfolios";

export function PortfolioEditItemActions({ portfolioId, item, onDocuments }: {
  portfolioId: string; item: PortfolioItemOut; onDocuments?: (item: PortfolioItemOut) => void;
}) {
  const { message } = App.useApp();
  const remove = useRemovePortfolioItem(portfolioId);
  const busy = item.status === "running" || Boolean(item.editing) || remove.isPending;
  return (
    <Space>
      <Button size="small" aria-label={`Состав документов ${item.source_name}`}
        disabled={busy || !item.check_id || item.status === "waiting" || !onDocuments}
        onClick={() => onDocuments?.(item)}>Документы</Button>
      <Popconfirm title="Убрать закупку из портфеля?"
        description="Проверка и документы останутся в истории."
        okText="Убрать" cancelText="Отмена" disabled={busy}
        onConfirm={() => remove.mutateAsync({ itemId: item.id, version: item.version ?? 0 }).then(
          () => { message.success("Закупка убрана из портфеля"); },
          (error: unknown) => { message.error(error instanceof ApiError ? error.message : "Не удалось убрать закупку"); },
        )}>
        <Button size="small" aria-label={`Удалить позицию ${item.source_name}`}
          danger disabled={busy} loading={remove.isPending}>Убрать</Button>
      </Popconfirm>
    </Space>
  );
}

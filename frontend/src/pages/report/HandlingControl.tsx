import { App, Select, Space, Typography } from "antd";
import type { KeyboardEvent, MouseEvent } from "react";

import { ApiError } from "../../api/client";
import { useUpdateHandling, type Handling } from "../../api/portfolios";
import type { FindingOut } from "../../api/types";

const HANDLING_OPTIONS: { value: Handling; label: string }[] = [
  { value: "new", label: "Не обработано" },
  { value: "in_progress", label: "Принято к работе" },
  { value: "resolved", label: "Отработано" },
  { value: "not_applicable", label: "Неактуально" },
];

export function HandlingControl({ finding, checkId }: { finding: FindingOut; checkId?: string }) {
  const { message } = App.useApp();
  const updateHandling = useUpdateHandling(checkId);

  const handleChange = (status: Handling) => {
    updateHandling.mutate(
      {
        findingId: finding.id,
        status,
        version: finding.handling_version ?? 0,
      },
      {
        onSuccess: () => {
          message.success("Статус обработки сохранён");
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            message.error("Конфликт: статус замечания был изменён другим пользователем. Данные обновлены");
          } else {
            message.error(err instanceof ApiError ? err.message : "Не удалось обновить статус обработки");
          }
        },
      },
    );
  };

  const stopEvent = (e: MouseEvent | KeyboardEvent) => {
    e.stopPropagation();
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        marginTop: 10,
      }}
      onClick={stopEvent}
      onKeyDown={stopEvent}
    >
      <span className="muted" style={{ fontSize: 12 }}>
        Обработка:
      </span>
      <Space size={6} align="center">
        <Select<Handling>
          size="small"
          aria-label="Статус обработки замечания"
          value={finding.handling_status ?? "new"}
          loading={updateHandling.isPending}
          disabled={updateHandling.isPending}
          onChange={handleChange}
          options={HANDLING_OPTIONS}
          style={{ minWidth: 160 }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          статус обработки не меняет оценку риска
        </Typography.Text>
      </Space>
    </div>
  );
}

import { CheckOutlined, CloseOutlined, EditOutlined } from "@ant-design/icons";
import { App, Button, Input, Space } from "antd";
import { useState } from "react";

import { ApiError } from "../../api/client";
import { useRenamePortfolio } from "../../api/portfolioEditing";

export interface PortfolioEditTitleProps {
  portfolioId: string;
  title: string;
  version?: number;
  canEdit?: boolean;
}

export function PortfolioEditTitle({
  portfolioId,
  title,
  version = 0,
  canEdit = false,
}: PortfolioEditTitleProps) {
  const { message } = App.useApp();
  const [isEditing, setIsEditing] = useState(false);
  const [value, setValue] = useState(title);
  const [editingVersion, setEditingVersion] = useState(version);
  const renameMutation = useRenamePortfolio(portfolioId);


  if (!canEdit) {
    return (
      <h1 className="page-title" style={{ margin: 0 }}>
        {title}
      </h1>
    );
  }

  const handleSave = () => {
    const trimmed = value.trim();
    if (!trimmed) {
      message.error("Название портфеля не может быть пустым");
      return;
    }
    if (trimmed === title) {
      setIsEditing(false);
      return;
    }

    renameMutation.mutate(
      { title: trimmed, version: editingVersion },
      {
        onSuccess: () => {
          message.success("Название портфеля обновлено");
          setIsEditing(false);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            message.error(
              "Конфликт версий: название портфеля было изменено другим пользователем. Данные обновлены",
            );
          } else {
            message.error(
              err instanceof ApiError ? err.message : "Не удалось переименовать портфель",
            );
          }
        },
      },
    );
  };

  const handleCancel = () => {
    setValue(title);
    setIsEditing(false);
  };

  if (isEditing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Input
          aria-label="Название портфеля"
          value={value}
          maxLength={200}
          autoFocus
          disabled={renameMutation.isPending}
          onChange={(e) => setValue(e.target.value)}
          onPressEnter={handleSave}
          style={{ maxWidth: 400, fontWeight: 600 }}
        />
        <Space size={4}>
          <Button
            type="primary"
            icon={<CheckOutlined />}
            loading={renameMutation.isPending}
            disabled={!value.trim()}
            onClick={handleSave}
          >
            Сохранить
          </Button>
          <Button
            icon={<CloseOutlined />}
            disabled={renameMutation.isPending}
            onClick={handleCancel}
          >
            Отмена
          </Button>
        </Space>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <h1 className="page-title" style={{ margin: 0 }}>
        {title}
      </h1>
      <Button
        type="text"
        size="small"
        icon={<EditOutlined />}
        aria-label="Переименовать портфель"
        onClick={() => {
          setValue(title);
          setEditingVersion(version);
          setIsEditing(true);
        }}
      />
    </div>
  );
}

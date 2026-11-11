import { DeleteOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App, Button, Popconfirm } from "antd";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "../../api/client";

export function PortfolioDelete({ id, disabled }: { id: string; disabled: boolean }) {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const client = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api<void>(`/api/portfolios/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["portfolios"] });
      navigate("/portfolios");
      message.success("Портфель удалён. Отдельные отчёты сохранены в истории проверок");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "Не удалось удалить портфель"),
  });
  return (
    <Popconfirm
      title="Удалить портфель?"
      description="Ожидающие исходники будут удалены. Отдельные отчёты останутся в истории проверок."
      onConfirm={() => remove.mutateAsync()}
      okText="Удалить"
      cancelText="Отмена"
      disabled={disabled}
    >
      <Button danger icon={<DeleteOutlined />} disabled={disabled} loading={remove.isPending}>
        Удалить портфель
      </Button>
    </Popconfirm>
  );
}

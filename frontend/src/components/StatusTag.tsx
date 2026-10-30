import { CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined, SyncOutlined } from "@ant-design/icons";
import { Tag } from "antd";

import type { CheckStatus } from "../api/types";

const STATUS: Record<CheckStatus, { label: string; color: string; icon: React.ReactNode }> = {
  queued: { label: "В очереди", color: "default", icon: <ClockCircleOutlined /> },
  running: { label: "Проверяется", color: "processing", icon: <SyncOutlined spin /> },
  done: { label: "Готово", color: "success", icon: <CheckCircleOutlined /> },
  done_with_warnings: { label: "Готово с предупреждениями", color: "warning", icon: <CheckCircleOutlined /> },
  failed: { label: "Не удалась", color: "error", icon: <CloseCircleOutlined /> },
};

export function StatusTag({ status }: { status: CheckStatus }) {
  const s = STATUS[status];
  return (
    <Tag color={s.color} icon={s.icon} style={{ marginInlineEnd: 0 }}>
      {s.label}
    </Tag>
  );
}

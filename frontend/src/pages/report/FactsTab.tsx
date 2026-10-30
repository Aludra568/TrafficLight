import { CheckCircleOutlined, EyeOutlined, WarningOutlined } from "@ant-design/icons";
import { Empty, Tooltip } from "antd";

import type { CardItem } from "../../api/types";
import { brand, lights } from "../../theme";

const SOURCE: Record<string, string> = {
  both: "Значение подтверждено двумя способами: грамматикой и моделью",
  rules: "Найдено грамматикой (без модели)",
  llm: "Найдено моделью, цитата проверена",
};

export function FactsTab({ card, onShow }: { card: CardItem[]; onShow: (item: CardItem) => void }) {
  if (!card.length) return <Empty description="Условия закупки в документах не найдены" />;
  return (
    <div style={{ background: brand.surface, border: `1px solid ${brand.border}`, borderRadius: 8 }}>
      {card.map((item, i) => {
        const conflict = item.conflict as { display?: string; quote?: string } | null;
        return (
          <div
            key={item.key}
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(150px, 38%) 1fr auto",
              gap: 12,
              alignItems: "start",
              padding: "10px 14px",
              borderTop: i ? `1px solid ${brand.border}` : "none",
            }}
          >
            <span className="muted">{item.label}</span>
            <div>
              <div className="num" style={{ fontWeight: 600 }}>
                {item.display}{" "}
                {item.source === "both" && (
                  <Tooltip title={SOURCE.both}>
                    <CheckCircleOutlined style={{ color: lights.green.solid, fontSize: 13 }} />
                  </Tooltip>
                )}
              </div>
              {conflict && (
                <div style={{ fontSize: 12, color: lights.yellow.text, marginTop: 2 }}>
                  <WarningOutlined /> Второй способ нашёл другое значение: {conflict.display ?? "—"} — проверьте
                  по документу
                </div>
              )}
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                «{item.quote.length > 140 ? `${item.quote.slice(0, 140)}…` : item.quote}»
              </div>
            </div>
            <Tooltip title={`Показать в документе: ${item.filename}, стр. ${item.page}`}>
              <button
                type="button"
                onClick={() => onShow(item)}
                aria-label={`Показать «${item.label}» в документе`}
                className="plain-button icon-target"
                style={{ color: brand.link }}
              >
                <EyeOutlined />
              </button>
            </Tooltip>
          </div>
        );
      })}
    </div>
  );
}

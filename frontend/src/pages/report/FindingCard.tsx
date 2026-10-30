import { CheckOutlined, CloseOutlined, EyeOutlined, SwapOutlined } from "@ant-design/icons";
import { Alert, Button, Collapse, Space, Tag, Tooltip, Typography } from "antd";
import { forwardRef, useState } from "react";

import type { EvidenceOut, FindingOut } from "../../api/types";
import { TrafficLight } from "../../components/TrafficLight";
import { KIND_LABELS } from "../../lib/catalog";
import { emphasize } from "../../lib/emphasis";
import { documentPlace, shortPlace } from "../../lib/format";
import { brand } from "../../theme";

/** Цитата длиннее — в развёрнутой карточке показываются первые 4 строки и кнопка «целиком». */
const LONG_QUOTE = 320;

function Quote({
  quote,
  where,
  fullWhere,
  onShow,
  clamp,
}: {
  quote: string;
  where: string; // коротко: «Проект контракта · п. 2.4 · стр. 1»
  fullWhere: string; // путь с разделами — в подсказке
  onShow?: () => void;
  clamp?: boolean;
}) {
  const [whole, setWhole] = useState(false);
  const long = !clamp && quote.length > LONG_QUOTE;
  const lines = clamp ? " clamp" : long && !whole ? " clamp-4" : "";
  return (
    <div>
      {/* значения в цитате отмечены тем же цветом, что маркер в документе */}
      <blockquote className={`quote${lines}`}>«{emphasize(quote, "quote-key")}»</blockquote>
      {long && (
        <button
          type="button"
          className="link-quiet"
          style={{ fontSize: 12, marginTop: 4 }}
          onClick={(event) => {
            event.stopPropagation();
            setWhole((v) => !v);
          }}
        >
          {whole ? "Свернуть цитату" : "Показать цитату целиком"}
        </button>
      )}
      {onShow ? (
        <Tooltip title={fullWhere} placement="bottomLeft">
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            className="finding-place"
            aria-label={`Показать в документе: ${fullWhere}`}
            onClick={(event) => {
              event.stopPropagation();
              onShow();
            }}
          >
            {where}
          </Button>
        </Tooltip>
      ) : (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }} title={fullWhere}>
          {where}
        </div>
      )}
    </div>
  );
}

interface Props {
  finding: FindingOut;
  selected: boolean;
  /** развёрнута: выбрана или первая, пока ничего не выбрано; остальные — заголовок, суть и цитата */
  expanded: boolean;
  filenames: Record<string, string>;
  onSelect: () => void;
  onShowEvidence: (evidence: EvidenceOut) => void;
  onFeedback: (verdict: "correct" | "false_positive") => void;
}

export const FindingCard = forwardRef<HTMLElement, Props>(function FindingCard(
  { finding, selected, expanded, filenames, onSelect, onShowEvidence, onFeedback },
  ref,
) {
  const contradiction = finding.kind === "contradiction" && finding.evidence.length > 1;
  const evidence = finding.evidence;
  const law = finding.legal_basis
    .map((ref) => [ref.act, ref.ref].filter(Boolean).join(" "))
    .filter(Boolean);
  return (
    // карточка — не кнопка: внутри неё свои кнопки (место цитаты, «Верно», «Почему это риск»),
    // а вложенные кнопки читалка экрана не различает. Мышью выбирает вся карточка, клавиатурой —
    // заголовок (настоящая кнопка): Tab, Enter или пробел
    <article
      ref={ref}
      aria-label={finding.title}
      aria-current={selected ? "true" : undefined}
      className={`finding-card${selected ? " selected" : ""}`}
      onClick={onSelect}
      style={{
        background: brand.surface,
        border: `1px solid ${brand.border}`,
        borderRadius: 8,
        padding: expanded ? "14px 16px" : "12px 16px",
      }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
        <TrafficLight light={finding.severity} iconOnly style={{ marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <button
              type="button"
              className="plain-button finding-title"
              aria-expanded={expanded}
              onClick={(event) => {
                event.stopPropagation();
                onSelect();
              }}
            >
              {finding.title}
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              {finding.rule_code}
            </span>
          </div>
          <div className={expanded ? undefined : "clamp-2"} style={{ marginTop: 2, color: brand.graphite }}>
            {emphasize(finding.message)}
          </div>
        </div>
      </div>

      {finding.needs_review && expanded && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 10, padding: "6px 10px" }}
          message="Требует проверки специалистом"
          description={
            finding.review?.comment
              ? `Второе мнение модели расходится: ${finding.review.comment}`
              : "Второе мнение модели расходится с первым — сверьте цитату с документом."
          }
        />
      )}

      {evidence.length > 0 ? (
        <div style={{ display: "grid", gap: 6, marginTop: 10 }}>
          {evidence.map((e, i) => (
            <div key={i}>
              {contradiction && i > 0 && (
                <div className="muted" style={{ fontSize: 12, margin: "2px 0 6px" }}>
                  <SwapOutlined style={{ marginRight: 6 }} />
                  расходится с
                </div>
              )}
              <Quote
                quote={e.quote}
                where={shortPlace(e, filenames[e.document_id])}
                fullWhere={documentPlace(e, filenames[e.document_id])}
                onShow={() => onShowEvidence(e)}
                clamp={!expanded}
              />
            </div>
          ))}
        </div>
      ) : finding.quote ? (
        <div style={{ marginTop: 10 }}>
          <Quote
            quote={finding.quote}
            where={shortPlace(finding, finding.document_id ? filenames[finding.document_id] : undefined)}
            fullWhere={documentPlace(finding, finding.document_id ? filenames[finding.document_id] : undefined)}
            clamp={!expanded}
          />
        </div>
      ) : (
        finding.kind === "absent" && (
          <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>
            {finding.search_scope
              ? `Проверено: ${finding.search_scope}`
              : finding.checked_documents.length
                ? `Искали в: ${finding.checked_documents.join(", ")}`
                : null}
          </div>
        )
      )}

      {expanded && (
        <>
          <Space size={[6, 6]} wrap style={{ marginTop: 10 }}>
            {/* «условие найдено» — обычный случай, метка нужна только необычным видам замечаний */}
            {finding.kind === "absent" && <Tag bordered={false}>{KIND_LABELS.absent}</Tag>}
            {finding.kind === "contradiction" && finding.category !== "consistency" && (
              <Tag bordered={false}>{KIND_LABELS.contradiction}</Tag>
            )}
            <Tag bordered={false}>{finding.category_title}</Tag>
            {law.map((item, i) => (
              <Tooltip key={i} title={finding.legal_basis[i]?.note ?? undefined}>
                <Tag bordered={false} color="blue">
                  {item}
                </Tag>
              </Tooltip>
            ))}
            {finding.rule_status !== "verified" && (
              <Tooltip title="Правило ещё не проверено юристом: замечание носит справочный характер">
                <Tag bordered={false} style={{ color: brand.gray }}>
                  правило — черновик
                </Tag>
              </Tooltip>
            )}
          </Space>

          <Collapse
            ghost
            size="small"
            style={{ marginTop: 6, marginLeft: -16, marginRight: -16 }}
            onChange={() => undefined}
            items={[
              ...(finding.why_risk ? [{ key: "why", label: "Почему это риск", children: <Typography.Paragraph style={{ margin: 0 }}>{finding.why_risk}</Typography.Paragraph> }] : []),
              ...(finding.recommendation ? [{ key: "todo", label: "Что делать", children: <Typography.Paragraph style={{ margin: 0 }}>{finding.recommendation}</Typography.Paragraph> }] : []),
              ...(finding.explanation ? [{ key: "explain", label: "Пояснение", children: <Typography.Paragraph style={{ margin: 0 }}>{finding.explanation}</Typography.Paragraph> }] : []),
            ]}
          />

          <div
            style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}
            onClick={(e) => e.stopPropagation()}
          >
            <span className="muted" style={{ fontSize: 12 }}>
              Замечание верное?
            </span>
            {/* два варианта одной отметки: выбранный выглядит одинаково. Красный — цвет
                критичного риска, на кнопке «Ошибка» он читался бы как ещё одно замечание */}
            <Button
              size="small"
              icon={<CheckOutlined />}
              type={finding.feedback === "correct" ? "primary" : "default"}
              aria-pressed={finding.feedback === "correct"}
              onClick={() => onFeedback("correct")}
            >
              Верно
            </Button>
            <Button
              size="small"
              icon={<CloseOutlined />}
              type={finding.feedback === "false_positive" ? "primary" : "default"}
              aria-pressed={finding.feedback === "false_positive"}
              onClick={() => onFeedback("false_positive")}
            >
              Ошибка
            </Button>
          </div>
        </>
      )}
      {!expanded && finding.needs_review && (
        <Tag bordered={false} color="warning" style={{ marginTop: 8 }}>
          требует проверки специалистом
        </Tag>
      )}
    </article>
  );
});

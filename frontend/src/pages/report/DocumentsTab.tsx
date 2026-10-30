import { FileOutlined, ScanOutlined } from "@ant-design/icons";
import { App, Button, Select, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { ApiError } from "../../api/client";
import { usePatchDocument } from "../../api/hooks";
import type { DocumentOut, FindingOut } from "../../api/types";
import { TrafficLight } from "../../components/TrafficLight";
import { DOC_TYPES } from "../../lib/catalog";
import { count } from "../../lib/format";
import { brand } from "../../theme";

const RANK: Record<string, number> = { green: 0, yellow: 1, red: 2 };

/**
 * Цвет и число замечаний документа — с учётом замечаний уровня пакета, где он процитирован
 * (противоречие ТЗ и контракта). Иначе документ из критичного противоречия показывался
 * зелёным «Нет рисков» в красном пакете.
 */
function documentSummary(doc: DocumentOut, findings: FindingOut[]) {
  const own = findings.filter(
    (f) => f.document_id === doc.id || f.evidence.some((e) => e.document_id === doc.id),
  );
  const light = own.reduce<string>(
    (worst, f) => ((RANK[f.severity] ?? 0) > (RANK[worst] ?? 0) ? f.severity : worst),
    doc.light ?? "green",
  );
  return { light, findings: own.length };
}

function DocumentRow({
  doc,
  findings,
  checkId,
  onOpen,
}: {
  doc: DocumentOut;
  findings: FindingOut[];
  checkId: string;
  onOpen: () => void;
}) {
  const summary = documentSummary(doc, findings);
  const { message } = App.useApp();
  const patch = usePatchDocument(checkId);
  const [editing, setEditing] = useState(false);
  const ready = doc.status === "ready";
  return (
    <div style={{ padding: "12px 14px", borderTop: `1px solid ${brand.border}` }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        {ready ? (
          // «без замечаний» — факт о документе; «нет рисков» он сказать не может: часть правил
          // могла быть не проверена, а итог — у пакета
          <TrafficLight
            light={summary.light as DocumentOut["light"]}
            size="sm"
            label={summary.light === "green" ? "Без замечаний" : undefined}
          />
        ) : (
          <Tag color="error">не обработан</Tag>
        )}
        <Button type="link" icon={<FileOutlined />} style={{ padding: 0 }} disabled={!ready} onClick={onOpen}>
          {doc.filename}
        </Button>
        <span className="muted" style={{ fontSize: 13 }}>
          {doc.pages ? count(doc.pages, ["страница", "страницы", "страниц"]) : ""}
          {doc.ocr_pages ? (
            <>
              {" · "}
              <ScanOutlined /> распознано {doc.ocr_pages}
            </>
          ) : null}
          {summary.findings ? ` · ${count(summary.findings, ["замечание", "замечания", "замечаний"])}` : ""}
        </span>
      </div>
      <div style={{ marginTop: 6, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        {editing ? (
          <Select
            size="small"
            autoFocus
            defaultOpen
            style={{ minWidth: 260 }}
            defaultValue={doc.doc_type}
            options={DOC_TYPES}
            onBlur={() => setEditing(false)}
            onChange={(value) =>
              patch.mutate(
                { documentId: doc.id, docType: value },
                {
                  onSuccess: (r) => {
                    message.success(r.hint);
                    setEditing(false);
                  },
                  onError: (e) => message.error(e instanceof ApiError ? e.message : "Не удалось"),
                },
              )
            }
          />
        ) : !ready && !doc.doc_type_certain ? (
          // документ не прочитан: спрашивать «похоже на …» не о чем — тип никто не определял
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            Тип не определён — документ не прочитан
          </Typography.Text>
        ) : doc.doc_type_certain ? (
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {doc.doc_type_title}{" "}
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setEditing(true)}>
              изменить
            </Button>
          </Typography.Text>
        ) : (
          <Space size={6}>
            <span style={{ fontSize: 13 }}>Похоже на «{doc.doc_type_title}». Верно?</span>
            <Button size="small" onClick={() => setEditing(true)}>
              Изменить тип
            </Button>
          </Space>
        )}
      </div>
      {(doc.problem || doc.warning) && (
        <Typography.Paragraph
          type={doc.problem ? "danger" : "warning"}
          style={{ margin: "6px 0 0", fontSize: 13 }}
        >
          {doc.problem ?? doc.warning}
        </Typography.Paragraph>
      )}
    </div>
  );
}

export function DocumentsTab({
  documents,
  findings,
  checkId,
  onOpen,
}: {
  documents: DocumentOut[];
  findings: FindingOut[];
  checkId: string;
  onOpen: (doc: DocumentOut) => void;
}) {
  return (
    <div style={{ background: brand.surface, border: `1px solid ${brand.border}`, borderRadius: 8, borderTop: "none" }}>
      {documents.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} findings={findings} checkId={checkId} onOpen={() => onOpen(doc)} />
      ))}
    </div>
  );
}

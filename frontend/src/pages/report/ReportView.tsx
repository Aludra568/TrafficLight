import { App, Drawer, Empty, Grid, Segmented, Select, Skeleton, Space, Tabs, Typography } from "antd";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useFeedback, useReport } from "../../api/hooks";
import type { CardItem, CheckOut, DocumentOut, EvidenceOut, FindingOut, Highlight } from "../../api/types";
import { PdfViewer, type ViewerTarget } from "../../components/PdfViewer";
import { TrafficLight } from "../../components/TrafficLight";
import { count } from "../../lib/format";
import { assessedOf, coverageText } from "../../lib/lights";
import { brand } from "../../theme";
import { CheckedTab } from "./CheckedTab";
import { DocumentsTab } from "./DocumentsTab";
import { FactsTab } from "./FactsTab";
import { FindingCard } from "./FindingCard";
import { ReportHeader } from "./ReportHeader";

const HEADER_HEIGHT = 56;

function highlightsOf(finding: FindingOut): Highlight[] {
  return (finding.highlights as unknown as Highlight[]) ?? [];
}

export function ReportView({ check }: { check: CheckOut }) {
  const { message } = App.useApp();
  const screens = Grid.useBreakpoint();
  const wide = Boolean(screens.lg);
  const report = useReport(check.id, true);
  const feedback = useFeedback(check.id);
  const [tab, setTab] = useState("risks");
  const [severity, setSeverity] = useState<"all" | "red" | "yellow">("all");
  const [category, setCategory] = useState<string | undefined>();
  const [documentFilter, setDocumentFilter] = useState<string | undefined>();
  const [selected, setSelected] = useState<string | null>(null);
  const [viewerDoc, setViewerDoc] = useState<string | null>(null);
  const [target, setTarget] = useState<ViewerTarget | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const cards = useRef<Map<string, HTMLElement>>(new Map());
  const jump = useRef(0);

  const data = report.data;
  const filenames = useMemo(
    () => Object.fromEntries((data?.documents ?? []).map((d) => [d.id, d.filename])),
    [data],
  );
  const findings = useMemo(() => {
    let list = data?.findings ?? [];
    if (severity !== "all") list = list.filter((f) => f.severity === severity);
    if (category) list = list.filter((f) => f.category === category);
    if (documentFilter)
      list = list.filter(
        (f) => f.document_id === documentFilter || f.evidence.some((e) => e.document_id === documentFilter),
      );
    // сначала критичные, потом «внимание» — список читается группами, а не сплошным потоком
    const rank = (f: FindingOut) => (f.severity === "red" ? 0 : 1);
    return [...list].sort((a, b) => rank(a) - rank(b));
  }, [data, severity, category, documentFilter]);
  const groupSize = useMemo(() => {
    const sizes: Record<string, number> = {};
    for (const f of findings) sizes[f.severity] = (sizes[f.severity] ?? 0) + 1;
    return sizes;
  }, [findings]);
  const grouped = Object.keys(groupSize).length > 1;

  const show = useCallback(
    (documentId: string, page: number, highlights: Highlight[], caption?: string, quote?: string) => {
      jump.current += 1;
      setViewerDoc(documentId);
      setTarget({ documentId, page, highlights, key: jump.current, caption, quote });
      if (!wide) setDrawerOpen(true);
    },
    [wide],
  );

  const selectFinding = useCallback(
    (finding: FindingOut) => {
      setSelected(finding.id);
      const highlights = highlightsOf(finding);
      const first = highlights[0];
      const documentId = first?.document_id ?? finding.evidence[0]?.document_id ?? finding.document_id;
      if (!documentId) {
        if (!wide) return;
        setViewerDoc(null);
        return;
      }
      const page = first?.page ?? finding.page_from ?? 1;
      show(documentId, page, highlights.filter((h) => h.document_id === documentId));
    },
    [show, wide],
  );

  const showEvidence = (finding: FindingOut, evidence: EvidenceOut) => {
    setSelected(finding.id);
    const highlights = highlightsOf(finding).filter((h) => h.document_id === evidence.document_id);
    show(evidence.document_id, highlights[0]?.page ?? evidence.page_from, highlights);
  };

  const showFact = (item: CardItem) =>
    show(item.document_id, item.page, [], item.display ? `${item.label} — ${item.display}` : item.label, item.quote);

  const openDocument = (doc: DocumentOut) => show(doc.id, 1, []);

  // J / K — следующее и предыдущее замечание (как в почте и трекерах задач)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const field = (event.target as HTMLElement).closest("input, textarea, [contenteditable]");
      if (field || event.ctrlKey || event.metaKey || event.altKey || tab !== "risks") return;
      if (event.key !== "j" && event.key !== "k" && event.key !== "о" && event.key !== "л") return;
      if (!findings.length) return;
      const index = findings.findIndex((f) => f.id === selected);
      const step = event.key === "j" || event.key === "о" ? 1 : -1;
      const next = findings[Math.min(findings.length - 1, Math.max(0, index + step))];
      selectFinding(next);
      cards.current.get(next.id)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [findings, selected, selectFinding, tab]);

  if (report.isLoading || !data) {
    return (
      <div className="page">
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }

  // пока ничего не выбрано — справа первый документ с замечаниями (а не пустая панель)
  const firstHighlighted = data.findings.find((f) => highlightsOf(f).length);
  const defaultTarget: ViewerTarget | null = firstHighlighted
    ? {
        documentId: highlightsOf(firstHighlighted)[0].document_id,
        page: highlightsOf(firstHighlighted)[0].page,
        highlights: highlightsOf(firstHighlighted).filter(
          (h) => h.document_id === highlightsOf(firstHighlighted)[0].document_id,
        ),
        key: 0,
      }
    : null;
  const shownDoc =
    viewerDoc ?? defaultTarget?.documentId ?? data.documents.find((d) => d.status === "ready")?.id ?? null;
  const shownTarget = target ?? defaultTarget;

  const categories = [...new Map(data.findings.map((f) => [f.category, f.category_title])).entries()];
  const assessed = assessedOf(data.rules_summary);
  const viewerHeight = `calc(100vh - ${HEADER_HEIGHT + 32}px)`;
  const viewer = (
    <PdfViewer
      key={shownDoc ?? "none"}
      documentId={shownDoc}
      filename={shownDoc ? filenames[shownDoc] : undefined}
      target={shownTarget}
      height={wide ? viewerHeight : "calc(100vh - 120px)"}
    />
  );

  const risks = (
    <div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Segmented
          value={severity}
          onChange={(v) => setSeverity(v as typeof severity)}
          options={[
            { value: "all", label: `Все ${data.findings.length}` },
            { value: "red", label: `Критично ${data.findings.filter((f) => f.severity === "red").length}` },
            { value: "yellow", label: `Внимание ${data.findings.filter((f) => f.severity === "yellow").length}` },
          ]}
        />
        <Select
          allowClear
          placeholder="Категория"
          style={{ minWidth: 170 }}
          value={category}
          onChange={setCategory}
          options={categories.map(([value, label]) => ({ value, label }))}
        />
        <Select
          allowClear
          placeholder="Документ"
          style={{ minWidth: 170 }}
          value={documentFilter}
          onChange={setDocumentFilter}
          options={data.documents.map((d) => ({ value: d.id, label: d.filename }))}
        />
      </Space>
      {findings.length === 0 ? (
        <Empty
          style={{ padding: 32, background: brand.surface, borderRadius: 8 }}
          description={
            data.findings.length ? (
              "По выбранным условиям замечаний нет"
            ) : (
              <>
                {check.light === "gray" ? "Замечаний нет, но риски не оценены." : "Рисков не найдено."}{" "}
                {coverageText(assessed, data.rules_summary.checked)}.
                <br />
                <button type="button" className="link-quiet" onClick={() => setTab("checked")}>
                  {data.rules_summary.not_checked ? "Что и почему не проверено" : "Что проверено"}
                </button>
              </>
            )
          }
        />
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {findings.map((finding, index) => (
            <Fragment key={finding.id}>
            {grouped && finding.severity !== findings[index - 1]?.severity && (
              <h3 className="findings-group">
                <TrafficLight light={finding.severity} iconOnly />
                {finding.severity === "red" ? "Критично" : "Внимание"} · {groupSize[finding.severity]}
              </h3>
            )}
            <FindingCard
              ref={(node) => {
                if (node) cards.current.set(finding.id, node);
                else cards.current.delete(finding.id);
              }}
              finding={finding}
              selected={finding.id === selected}
              expanded={finding.id === selected || (index === 0 && !findings.some((f) => f.id === selected))}
              filenames={filenames}
              onSelect={() => selectFinding(finding)}
              onShowEvidence={(e) => showEvidence(finding, e)}
              onFeedback={(verdict) =>
                feedback.mutate(
                  { findingId: finding.id, verdict },
                  { onSuccess: () => message.success("Спасибо — отметка учтётся в качестве правил") },
                )
              }
            />
            </Fragment>
          ))}
          {data.rules_summary.not_checked > 0 && (
            <button type="button" className="not-checked-row" onClick={() => setTab("checked")}>
              <TrafficLight light="gray" iconOnly />
              <span>
                Не проверено:{" "}
                {count(data.rules_summary.not_checked, ["правило", "правила", "правил"])}
              </span>
              <span className="muted">почему?</span>
            </button>
          )}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, textAlign: "center", margin: "8px 0 0" }}>
            Клавиши J и K — следующее и предыдущее замечание
          </Typography.Paragraph>
        </div>
      )}
    </div>
  );

  return (
    <div>
      <ReportHeader check={check} report={data} onOpenTab={setTab} />
      <div
        className="report-band"
        style={{
          display: "grid",
          // minmax(0, …): широкое содержимое (фильтры, длинные пути) не распирает колонку шире экрана
          gridTemplateColumns: wide ? "minmax(440px, 5fr) minmax(0, 7fr)" : "minmax(0, 1fr)",
          gap: 20,
          alignItems: "start",
        }}
      >
        <div style={{ minWidth: 0 }}>
          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              { key: "risks", label: `Риски ${data.findings.length}`, children: risks },
              { key: "card", label: "Карточка закупки", children: <FactsTab card={data.card} onShow={showFact} /> },
              {
                key: "documents",
                label: `Документы ${data.documents.length}`,
                children: <DocumentsTab documents={data.documents} findings={data.findings} checkId={check.id} onOpen={openDocument} />,
              },
              { key: "checked", label: "Что проверено", children: <CheckedTab rules={data.rules} otherLaw={data.rules_summary.other_law ?? 0} /> },
            ]}
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 16 }}>
            Отчёт — подсказка для специалиста, а не юридическое заключение. Цитаты взяты из документов и
            проверены программно; правила со статусом «черновик» ещё не проверены юристом.
          </Typography.Paragraph>
        </div>
        {wide && (
          <div
            style={{
              position: "sticky",
              top: HEADER_HEIGHT + 16,
              border: `1px solid ${brand.border}`,
              borderRadius: 8,
              overflow: "hidden",
            }}
          >
            {viewer}
          </div>
        )}
      </div>
      {!wide && (
        <Drawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width="100%"
          title="Документ"
          styles={{ body: { padding: 0 } }}
        >
          {viewer}
        </Drawer>
      )}
    </div>
  );
}

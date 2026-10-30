import { Segmented, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";

import type { RuleResultOut } from "../../api/types";
import { emphasize } from "../../lib/emphasis";
import { count } from "../../lib/format";

const OUTCOME: Record<RuleResultOut["outcome"], { label: string; color: string }> = {
  violation: { label: "Есть замечание", color: "error" },
  absent: { label: "Условие не найдено", color: "warning" },
  ok: { label: "В порядке", color: "success" },
  not_checked: { label: "Не проверено", color: "default" },
};

/** На чём основано «в порядке»: зелёный — утверждение, и у него должно быть основание. */
const BASIS: Record<string, string> = {
  evidence: "по цитате из документа",
  absence: "формулировки риска нет в прочитанном тексте",
  unconfirmed: "со слов модели, цитата не подтвердилась — в полноту проверки не входит",
  coverage: "по поштучной проверке: каждый пункт документа проверен, нарушений нет",
};

function OutcomeTag({ row }: { row: RuleResultOut }) {
  const unconfirmed = row.outcome === "ok" && row.basis === "unconfirmed";
  const look = OUTCOME[row.outcome];
  return (
    <Tag color={unconfirmed ? "default" : look.color} bordered={false}>
      {unconfirmed ? "В порядке?" : look.label}
    </Tag>
  );
}

/** Все правила набора с исходом: так видно полноту проверки, а «не проверено» — с причиной. */
export function CheckedTab({ rules, otherLaw = 0 }: { rules: RuleResultOut[]; otherLaw?: number }) {
  const [filter, setFilter] = useState<string>("all");
  const shown = useMemo(() => {
    if (filter === "all") return rules;
    if (filter === "violation") return rules.filter((r) => r.outcome === "violation" || r.outcome === "absent");
    return rules.filter((r) => r.outcome === filter);
  }, [rules, filter]);
  const counts = (outcome: string) => rules.filter((r) => r.outcome === outcome).length;
  return (
    <div>
      <Segmented
        style={{ marginBottom: 12 }}
        value={filter}
        onChange={(v) => setFilter(String(v))}
        options={[
          { value: "all", label: `Все ${rules.length}` },
          { value: "violation", label: `С замечанием ${counts("violation") + counts("absent")}` },
          { value: "ok", label: `В порядке ${counts("ok")}` },
          { value: "not_checked", label: `Не проверено ${counts("not_checked")}` },
        ]}
      />
      <Table
        size="small"
        rowKey="rule_code"
        pagination={false}
        scroll={{ x: 560 }}
        dataSource={shown}
        columns={[
          { title: "Код", dataIndex: "rule_code", width: 84, render: (v: string) => <span className="num">{v}</span> },
          {
            title: "Правило",
            dataIndex: "title",
            render: (title: string, row: RuleResultOut) => (
              <div>
                <div>{title}</div>
                {row.reason && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {row.reason}
                  </Typography.Text>
                )}
                {row.outcome === "ok" && row.basis && BASIS[row.basis] && (
                  <div className="muted" style={{ fontSize: 12 }}>
                    В порядке {BASIS[row.basis]}
                  </div>
                )}
                {row.outcome === "ok" && row.evidence?.[0] && (
                  <div className="muted" style={{ fontSize: 12 }}>
                    Основание: «{emphasize(row.evidence[0]!.quote.slice(0, 120), "quote-key")}
                    {row.evidence[0]!.quote.length > 120 ? "…" : ""}»
                  </div>
                )}
              </div>
            ),
          },
          {
            title: "Итог",
            dataIndex: "outcome",
            width: 170,
            render: (_: unknown, row: RuleResultOut) => <OutcomeTag row={row} />,
          },
        ]}
      />
      {otherLaw > 0 && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
          Ещё {count(otherLaw, ["правило", "правила", "правил"])} — для другого закона о закупках: к
          этой закупке не относятся и в полноту проверки не входят.
        </Typography.Paragraph>
      )}
    </div>
  );
}

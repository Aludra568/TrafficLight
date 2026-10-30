/** Типы ответов API — сгенерированы из OpenAPI бэкенда (`npm run api-types`). */
import type { components } from "./schema";

type S = components["schemas"];

export type User = S["UserOut"];
export type TokenOut = S["TokenOut"];
export type CheckOut = S["CheckOut"];
export type CheckBrief = S["CheckBrief"];
export type CheckListItem = S["CheckListItem"];
export type CheckPage = S["CheckPage"];
export type CheckCreated = S["CheckCreated"];
export type EisTask = S["EisTask"];
export type EisStatus = S["EisStatus"];
export type StageOut = S["StageOut"];
export type ReportOut = S["ReportOut"];
export type FindingOut = S["FindingOut"];
export type EvidenceOut = S["EvidenceOut"];
export type CardItem = S["CardItem"];
export type DocumentOut = S["DocumentOut"];
export type RuleResultOut = S["RuleResultOut"];
export type RulesSummary = S["RulesSummary"];
export type RiskOut = S["RiskOut"];
export type RulesetOut = S["RulesetOut"];
export type RuleListItem = S["RuleListItem"];
export type RuleDetail = S["RuleDetail"];
export type QualityOut = S["QualityOut"];

export type Light = S["Light"];
export type Severity = "red" | "yellow";
export type CheckStatus = CheckOut["status"];

/** Подсветка цитаты: страница и прямоугольники в пунктах PDF (начало — левый верхний угол). */
export interface Highlight {
  document_id: string;
  page: number;
  rects: [number, number, number, number][];
}

export interface DiffPart {
  op: "equal" | "delete" | "insert";
  text: string;
}

export interface ComparedUnit {
  number: string | null;
  path: string;
  page: number;
  text: string;
}

export interface UnitChange {
  status: "changed" | "renumbered" | "added" | "removed";
  old: ComparedUnit | null;
  new: ComparedUnit | null;
  diff: DiffPart[];
}

export interface Comparison {
  old_check_id: string;
  new_check_id: string;
  documents: {
    doc_type: string;
    doc_type_title: string;
    old_filename: string | null;
    new_filename: string | null;
    status: "changed" | "same" | "added" | "removed";
    changes: UnitChange[];
  }[];
  card: {
    key: string;
    label: string;
    old: string | null;
    new: string | null;
    old_quote: string | null;
    new_quote: string | null;
  }[];
  risks: {
    appeared: RiskChange[];
    resolved: RiskChange[];
    severity_changed: (RiskChange & { old_severity: Severity })[];
  };
}

export interface RiskChange {
  rule_code: string;
  title: string;
  severity: Severity;
  quote: string | null;
}

export interface Edition {
  check_id: string;
  package_title: string;
  created_at: string;
  light: Light | null;
  procurement_number: string;
}

export const ACTIVE_STATUSES: CheckStatus[] = ["queued", "running"];

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { ApiError, api } from "./client";
import {
  ACTIVE_STATUSES,
  type CheckCreated,
  type CheckOut,
  type CheckPage,
  type EisStatus,
  type EisTask,
  type Comparison,
  type Edition,
  type ReportOut,
  type RuleDetail,
  type RuleListItem,
  type RulesetOut,
} from "./types";

export const keys = {
  me: ["me"] as const,
  checks: (params: object) => ["checks", params] as const,
  check: (id: string) => ["check", id] as const,
  report: (id: string) => ["report", id] as const,
  editions: (id: string) => ["editions", id] as const,
  compare: (id: string, other: string) => ["compare", id, other] as const,
  rulesets: ["rulesets"] as const,
  rules: ["rules"] as const,
  rule: (code: string) => ["rule", code] as const,
  settings: ["settings"] as const,
};

// ─────────────── проверки ───────────────

export interface ChecksQuery {
  page: number;
  pageSize: number;
  light?: string;
  search?: string;
}

export function useChecks(query: ChecksQuery) {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) });
  if (query.light) params.set("light", query.light);
  if (query.search?.trim()) params.set("q", query.search.trim());
  return useQuery({
    queryKey: keys.checks(query),
    queryFn: () => api<CheckPage>(`/api/checks?${params}`),
    placeholderData: keepPreviousData, // при смене страницы или поиска таблица не мигает пустотой
    // пока в истории есть проверки «в работе» — обновляем раз в 5 секунд
    refetchInterval: (q) =>
      q.state.data?.items.some((i) => ACTIVE_STATUSES.includes(i.status)) ? 5000 : false,
  });
}

export function useCheck(id: string) {
  return useQuery({
    queryKey: keys.check(id),
    queryFn: () => api<CheckOut>(`/api/checks/${id}`),
    // запасной путь, если поток событий недоступен: опрос, пока проверка идёт
    refetchInterval: (q) =>
      q.state.data && ACTIVE_STATUSES.includes(q.state.data.status) ? 3000 : false,
  });
}

export function useReport(id: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.report(id),
    queryFn: () => api<ReportOut>(`/api/checks/${id}/report`),
    enabled,
  });
}

export function useCreateCheck() {
  return useMutation({
    mutationFn: ({ files, ruleset }: { files: File[]; ruleset?: string }) => {
      const form = new FormData();
      if (files.length === 1) form.append("file", files[0]);
      else files.forEach((f) => form.append("files", f));
      if (ruleset) form.append("ruleset_code", ruleset);
      return api<CheckCreated>("/api/checks", { method: "POST", body: form });
    },
  });
}

/**
 * Закупка из ЕИС по номеру: задача скачивает документы (секунды — минуты), интерфейс ждёт её
 * опросом раз в 2 с и отдаёт id готовой проверки. Ошибка ЕИС приходит текстом по-русски.
 */
export function useEisCheck() {
  return useMutation({
    mutationFn: async (number: string): Promise<CheckCreated> => {
      const { task_id } = await api<EisTask>("/api/checks/eis", {
        method: "POST",
        json: { number },
      });
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const status = await api<EisStatus>(`/api/checks/eis/${encodeURIComponent(task_id)}`);
        if (status.status === "done" && status.check_id && status.package_id) {
          return { check_id: status.check_id, package_id: status.package_id };
        }
        if (status.status === "failed") {
          throw new ApiError(502, "eis_failed", status.error ?? "Не удалось загрузить закупку из ЕИС");
        }
      }
      throw new ApiError(504, "eis_timeout", "ЕИС отвечает слишком долго — попробуйте позже");
    },
  });
}

export function useDemoCheck() {
  return useMutation({
    mutationFn: () => api<CheckCreated>("/api/checks/demo", { method: "POST" }),
  });
}

export function useRerun() {
  return useMutation({
    mutationFn: (id: string) => api<CheckCreated>(`/api/checks/${id}/rerun`, { method: "POST" }),
  });
}

export function useEditions(id: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.editions(id),
    queryFn: () => api<Edition[]>(`/api/checks/${id}/editions`),
    enabled,
  });
}

export function useCompare(id: string, other: string) {
  return useQuery({
    queryKey: keys.compare(id, other),
    queryFn: () => api<Comparison>(`/api/checks/${id}/compare?with=${other}`),
  });
}

export function useFeedback(checkId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ findingId, verdict }: { findingId: string; verdict: "correct" | "false_positive" }) =>
      api<void>(`/api/findings/${findingId}/feedback`, { method: "POST", json: { verdict } }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.report(checkId) }),
  });
}

export function usePatchDocument(checkId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ documentId, docType }: { documentId: string; docType: string }) =>
      api<{ hint: string }>(`/api/documents/${documentId}`, {
        method: "PATCH",
        json: { doc_type: docType },
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.report(checkId) }),
  });
}

// ─────────────── поток событий проверки (SSE) ───────────────

/**
 * Ход проверки «вживую»: билет (2 минуты, только эта проверка) → EventSource.
 * Обрыв — новый билет и переподключение (до 3 раз), дальше остаётся опрос из useCheck.
 */
export function useCheckEvents(id: string, active: boolean) {
  const client = useQueryClient();
  useEffect(() => {
    if (!active) return;
    let source: EventSource | null = null;
    let closed = false;
    let attempts = 0;

    const connect = async () => {
      if (closed || attempts >= 3) return;
      attempts += 1;
      try {
        const { ticket } = await api<{ ticket: string }>(`/api/checks/${id}/events/ticket`, {
          method: "POST",
        });
        if (closed) return;
        source = new EventSource(`/api/checks/${id}/events?ticket=${encodeURIComponent(ticket)}`);
        const update = (event: MessageEvent) => {
          client.setQueryData(keys.check(id), JSON.parse(event.data) as CheckOut);
        };
        source.addEventListener("progress", update);
        source.addEventListener("done", (event) => {
          update(event as MessageEvent);
          source?.close();
          void client.invalidateQueries({ queryKey: keys.report(id) });
          void client.invalidateQueries({ queryKey: ["checks"] });
        });
        source.onerror = () => {
          source?.close();
          window.setTimeout(connect, 1500);
        };
      } catch {
        /* билет не выдан — работает опрос */
      }
    };
    void connect();
    return () => {
      closed = true;
      source?.close();
    };
  }, [id, active, client]);
}

// ─────────────── правила, качество, настройки ───────────────

export function useRulesets() {
  return useQuery({ queryKey: keys.rulesets, queryFn: () => api<RulesetOut[]>("/api/rulesets") });
}

export function useRules(enabled = true) {
  return useQuery({ queryKey: keys.rules, queryFn: () => api<RuleListItem[]>("/api/rules"), enabled });
}

export function useRule(code: string | null) {
  return useQuery({
    queryKey: keys.rule(code ?? ""),
    queryFn: () => api<RuleDetail>(`/api/rules/${code}`),
    enabled: Boolean(code),
  });
}

export function useSettings() {
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => api<Record<string, unknown>>("/api/settings"),
    staleTime: 5 * 60_000,
  });
}

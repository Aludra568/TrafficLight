import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, download } from "./client";
import { portfolioExportName } from "../lib/portfolioFilename";
import type { Handling, Light } from "./types";

export type { Handling };

export interface HandlingIn {
  status: Handling;
  version: number;
}

export interface HandlingOut {
  status: Handling;
  version: number;
  updated_at: string | null;
}

export interface PortfolioItemOut {
  id: string;
  position: number;
  source_type: string;
  source_name: string;
  check_id: string | null;
  status: "waiting" | "running" | "done" | "failed";
  light: Light | null;
  progress: number;
  error: string | null;
  findings: number;
  version?: number;
  editing?: boolean;
  edit_error?: string | null;
}

export interface RepeatedRisk {
  rule_code: string;
  title: string;
  procurements: number;
}

export interface PortfolioFinding {
  id: string;
  check_id: string;
  source_name: string;
  rule_code: string;
  title: string;
  severity: string;
  category: string;
  category_title?: string;
  quote: string | null;
  document_id: string | null;
  page: number | null;
  handling_status: Handling;
}

export interface PortfolioOut {
  id: string;
  title: string;
  created_at: string;
  light: Light;
  complete: boolean;
  total: number;
  done: number;
  running: number;
  waiting: number;
  failed: number;
  distribution: Record<string, number>;
  items: PortfolioItemOut[];
  repeated_risks: RepeatedRisk[];
  findings: PortfolioFinding[];
  title_version?: number;
  can_edit?: boolean;
}

export interface PortfolioBrief {
  id: string;
  title: string;
  created_at: string;
  total: number;
}

export interface PortfolioList {
  items: PortfolioBrief[];
  total: number;
}

export interface PortfolioCreated {
  id: string;
  dispatch_error?: string | null;
}

export const portfolioKeys = {
  all: ["portfolios"] as const,
  list: (page: number, pageSize: number) => ["portfolios", { page, pageSize }] as const,
  detail: (id: string) => ["portfolio", id] as const,
};

export function generateUuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function usePortfolios(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: portfolioKeys.list(page, pageSize),
    queryFn: () => api<PortfolioList>(`/api/portfolios?page=${page}&page_size=${pageSize}`),
    placeholderData: keepPreviousData,
    refetchInterval: 5000,
  });
}

export function usePortfolio(id: string | undefined) {
  return useQuery({
    queryKey: portfolioKeys.detail(id ?? ""),
    queryFn: () => api<PortfolioOut>(`/api/portfolios/${id}`),
    enabled: Boolean(id),
    refetchInterval: (q) => (q.state.data && !q.state.data.complete ? 3000 : false),
  });
}

export function useCreatePortfolio() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      title,
      requestKey,
      files,
      numbers,
    }: {
      title: string;
      requestKey: string;
      files?: File[];
      numbers?: string[];
    }) => {
      const form = new FormData();
      form.append("title", title);
      form.append("request_key", requestKey);
      if (files && files.length > 0) {
        files.forEach((f) => form.append("files", f));
      } else if (numbers && numbers.length > 0) {
        form.append("numbers", numbers.join("\n"));
      }
      return api<PortfolioCreated>("/api/portfolios", { method: "POST", body: form });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
  });
}

export function useRetryPortfolio(portfolioId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api<PortfolioCreated>(`/api/portfolios/${portfolioId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
  });
}

export function useUpdateHandling(checkId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ findingId, status, version }: { findingId: string; status: Handling; version: number }) =>
      api<HandlingOut>(`/api/findings/${findingId}/handling`, {
        method: "PATCH",
        json: { status, version },
      }),
    onSuccess: () => {
      if (checkId) {
        void queryClient.invalidateQueries({ queryKey: ["report", checkId] });
      }
      void queryClient.invalidateQueries({ queryKey: ["report"] });
      void queryClient.invalidateQueries({ queryKey: ["portfolio"] });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        if (checkId) {
          void queryClient.invalidateQueries({ queryKey: ["report", checkId] });
        }
        void queryClient.invalidateQueries({ queryKey: ["report"] });
        void queryClient.invalidateQueries({ queryKey: ["portfolio"] });
      }
    },
  });
}

export function downloadPortfolioExcel(id: string, title: string): Promise<void> {
  return download(`/api/portfolios/${id}/export.xlsx`, portfolioExportName(title, "xlsx"));
}

export function downloadPortfolioDocx(id: string, title: string): Promise<void> {
  return download(`/api/portfolios/${id}/export.docx`, portfolioExportName(title, "docx"));
}

export function downloadCheckExcel(checkId: string, title: string): Promise<void> {
  return download(`/api/checks/${checkId}/export.xlsx`, `Светофор — замечания — ${title}.xlsx`);
}

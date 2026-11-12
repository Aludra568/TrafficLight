import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "./client";
import { portfolioKeys, type PortfolioCreated, type PortfolioOut } from "./portfolios";

export interface PortfolioDocument {
  id: string;
  filename: string;
  status: string;
}

export interface ItemDocumentsOut {
  item_version: number;
  editable: boolean;
  documents: PortfolioDocument[];
}

export interface RenamePortfolioIn {
  title: string;
  version: number;
}

export interface AddPortfolioItemsParams {
  requestKey: string;
  files?: File[];
  numbers?: string[];
  bundle?: boolean;
}

export interface RemovePortfolioItemParams {
  itemId: string;
  version: number;
}

export interface UpdateItemDocumentsParams {
  requestKey: string;
  version: number;
  removeIds: string[];
  files?: File[];
}

export const portfolioEditingKeys = {
  itemDocuments: (portfolioId: string, itemId: string) =>
    ["portfolio", portfolioId, "item", itemId, "documents"] as const,
};

export function useRenamePortfolio(portfolioId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ title, version }: RenamePortfolioIn) =>
      api<PortfolioOut>(`/api/portfolios/${portfolioId}`, {
        method: "PATCH",
        json: { title, version },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
      }
    },
  });
}

export function useAddPortfolioItems(portfolioId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ requestKey, files, numbers, bundle }: AddPortfolioItemsParams) => {
      const form = new FormData();
      form.append("request_key", requestKey);
      form.append("bundle", bundle ? "true" : "false");
      if (files && files.length > 0) {
        files.forEach((f) => form.append("files", f));
      } else if (numbers && numbers.length > 0) {
        form.append("numbers", numbers.join("\n"));
      }
      return api<PortfolioCreated>(`/api/portfolios/${portfolioId}/items`, {
        method: "POST",
        body: form,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
      }
    },
  });
}

export function useRemovePortfolioItem(portfolioId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, version }: RemovePortfolioItemParams) =>
      api<void>(`/api/portfolios/${portfolioId}/items/${itemId}?version=${version}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
      }
    },
  });
}

export function useItemDocuments(portfolioId?: string, itemId?: string, enabled = true) {
  return useQuery({
    queryKey: portfolioEditingKeys.itemDocuments(portfolioId ?? "", itemId ?? ""),
    queryFn: () =>
      api<ItemDocumentsOut>(`/api/portfolios/${portfolioId}/items/${itemId}/documents`),
    enabled: Boolean(portfolioId && itemId && enabled),
  });
}

export function useUpdateItemDocuments(portfolioId: string, itemId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ requestKey, version, removeIds, files }: UpdateItemDocumentsParams) => {
      const form = new FormData();
      form.append("request_key", requestKey);
      form.append("version", String(version));
      form.append("remove_ids", JSON.stringify(removeIds));
      if (files && files.length > 0) {
        files.forEach((f) => form.append("files", f));
      }
      return api<PortfolioCreated>(`/api/portfolios/${portfolioId}/items/${itemId}/documents`, {
        method: "POST",
        body: form,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
      void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
      void queryClient.invalidateQueries({
        queryKey: portfolioEditingKeys.itemDocuments(portfolioId, itemId),
      });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.detail(portfolioId) });
        void queryClient.invalidateQueries({ queryKey: portfolioKeys.all });
        void queryClient.invalidateQueries({
          queryKey: portfolioEditingKeys.itemDocuments(portfolioId, itemId),
        });
      }
    },
  });
}

import { ApiError, download, request, saveResponse } from "../api/client";

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

export function downloadDocx(checkId: string, title: string): Promise<void> {
  return download(`/api/checks/${checkId}/export?format=docx`, `Светофор — отчёт — ${title}.docx`, {
    method: "POST",
  });
}

/** PDF готовится задачей (LibreOffice): 202 — ждём и спрашиваем снова, до минуты. */
export async function downloadPdf(checkId: string, title: string): Promise<void> {
  const fallback = `Светофор — отчёт — ${title}.pdf`;
  const first = await request(`/api/checks/${checkId}/export?format=pdf`, { method: "POST" });
  if (first.status === 200) return saveResponse(first, fallback);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await sleep(1500);
    const ready = await request(`/api/checks/${checkId}/export/pdf`);
    if (ready.status === 200) return saveResponse(ready, fallback);
  }
  throw new ApiError(0, "timeout", "PDF не успел подготовиться — скачайте отчёт в Word");
}

export function downloadClarification(checkId: string, title: string, findingIds?: string[]) {
  return download(
    `/api/checks/${checkId}/clarification`,
    `Запрос на разъяснение — ${title}.docx`,
    { method: "POST", json: findingIds ? { finding_ids: findingIds } : {} },
  );
}

export function downloadComparison(checkId: string, otherId: string, title: string) {
  return download(
    `/api/checks/${checkId}/compare.docx?with=${otherId}`,
    `Светофор — изменения — ${title}.docx`,
  );
}

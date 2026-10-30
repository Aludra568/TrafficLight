import { FileSearchOutlined, MinusOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Empty, Spin, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
// текстовый слой прозрачен и лежит поверх холста: текст документа можно выделить и скопировать
import "react-pdf/dist/Page/TextLayer.css";

import { tokenStore } from "../api/client";
import type { Highlight } from "../api/types";
import { brand } from "../theme";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

/** Куда смотреть: документ, страница, прямоугольники цитаты; `key` — новый переход (мигание). */
export interface ViewerTarget {
  documentId: string;
  page: number;
  highlights: Highlight[];
  key: number;
  caption?: string;
  /** цитата: если прямоугольников нет (условие карточки), её ищут в текстовом слое страницы */
  quote?: string;
}

/** Прямоугольник на странице в CSS-пикселях: left, top, width, height. */
type Box = [number, number, number, number];

const fold = (text: string) => text.toLowerCase().replace(/ё/g, "е").replace(/[«»"]/g, " ");

/** Пробелы, мягкие переносы и невидимые символы при сравнении не учитываются. */
const SKIP = /[\s\u00ad\u200b-\u200d\ufeff]/;

/**
 * Где на странице стоит цитата: по тексту слоя pdf.js без учёта регистра и пробелов. pdf.js режет
 * строку на куски как угодно («Прое» + «кт»), а пробел между кусками может быть, а может нет —
 * поэтому сравниваются только значащие символы. Не нашлась целиком — ищем её начало.
 */
function quoteBoxes(page: HTMLElement, quote: string): Box[] {
  const spans = [...page.querySelectorAll<HTMLElement>(".textLayer span, .react-pdf__Page__textContent span")];
  let text = "";
  const map: Array<[Text, number]> = [];
  for (const span of spans) {
    const node = span.firstChild;
    if (!(node instanceof Text)) continue;
    const folded = fold(node.data);
    for (let i = 0; i < folded.length; i++) {
      if (SKIP.test(folded[i])) continue;
      text += folded[i];
      map.push([node, i]);
    }
  }
  const wanted = [...fold(quote).replace(/…$/, "")].filter((ch) => !SKIP.test(ch)).join("");
  if (!wanted) return [];
  let start = text.indexOf(wanted);
  let length = wanted.length;
  if (start < 0 && wanted.length > 30) {
    length = 30;
    start = text.indexOf(wanted.slice(0, length));
  }
  if (start < 0) return [];
  const origin = page.getBoundingClientRect();
  const boxes: Box[] = [];
  for (let i = start; i < start + length; ) {
    const [node, from] = map[i];
    let j = i;
    while (j + 1 < start + length && map[j + 1][0] === node) j++;
    const range = document.createRange();
    range.setStart(node, Math.min(from, node.data.length));
    range.setEnd(node, Math.min(map[j][1] + 1, node.data.length));
    for (const r of range.getClientRects()) {
      if (r.width > 1) boxes.push([r.left - origin.left, r.top - origin.top, r.width, r.height]);
    }
    i = j + 1;
  }
  return boxes;
}

interface Props {
  documentId: string | null;
  filename?: string;
  target: ViewerTarget | null;
  height: string;
}

const RENDER_AROUND = 2; // сколько страниц рисовать вокруг видимых: остальное — заглушки
const PAGE_GAP = 12;

/**
 * Просмотр PDF с подсветкой цитат. 50+ страниц не рисуются разом: холст каждой страницы —
 * мегабайты памяти, поэтому рисуются только видимые и соседние, остальные — заглушки
 * нужного размера (прокрутка не прыгает).
 */
export function PdfViewer({ documentId, filename, target, height }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [width, setWidth] = useState(640);
  const [zoom, setZoom] = useState(1);
  const [pages, setPages] = useState(0);
  const [aspect, setAspect] = useState(1.414); // высота / ширина, по первой странице
  const [pointWidth, setPointWidth] = useState<Record<number, number>>({});
  const [visible, setVisible] = useState<Set<number>>(new Set([1]));
  // цитата, найденная в тексте страницы (у условий карточки нет прямоугольников с сервера)
  const [found, setFound] = useState<{ key: number; page: number; boxes: Box[] } | null>(null);

  // ширина колонки → ширина страницы
  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(280, Math.floor(entry.contentRect.width) - 32));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const file = useMemo(
    () =>
      documentId
        ? {
            url: `/api/documents/${documentId}/pdf`,
            httpHeaders: { Authorization: `Bearer ${tokenStore.get() ?? ""}` },
          }
        : null,
    [documentId],
  );

  // какие страницы видны — рисуем их и соседние
  useEffect(() => {
    const root = scroller.current;
    if (!root || pages === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          const next = new Set(prev);
          for (const entry of entries) {
            const page = Number((entry.target as HTMLElement).dataset.page);
            if (entry.isIntersecting) next.add(page);
            else next.delete(page);
          }
          return next;
        });
      },
      { root, rootMargin: "200px 0px" },
    );
    pageRefs.current.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [pages, documentId]);

  const pageWidth = Math.round(width * zoom);
  const current = visible.size ? Math.min(...visible) : 1;
  const near = useCallback(
    (page: number) => [...visible].some((v) => Math.abs(v - page) <= RENDER_AROUND),
    [visible],
  );

  // Поиск цитаты в текстовом слое — один раз на переход и ширину страницы. Найденное попадает
  // в состояние и перерисовывает страницу; без этой отметки и стабильного обработчика слой
  // перерисовывался бы, а поиск запускался по кругу.
  const searched = useRef("");
  const scrolledTo = useRef(-1);
  const locate = useRef<(page: number) => void>(() => undefined);
  useEffect(() => {
    locate.current = (page: number) => {
      if (!target?.quote || target.documentId !== documentId || target.page !== page) return;
      if (target.highlights.some((h) => h.page === page)) return;
      const node = pageRefs.current.get(page);
      const mark = `${target.key}:${pageWidth}`;
      if (!node || searched.current === mark) return;
      const boxes = quoteBoxes(node, target.quote);
      if (!boxes.length) return; // слой ещё не готов — дождёмся onRenderTextLayerSuccess
      searched.current = mark;
      setFound({ key: target.key, page, boxes });
      if (scrolledTo.current !== target.key) {
        scrolledTo.current = target.key;
        scroller.current?.scrollTo({ top: Math.max(0, node.offsetTop + boxes[0][1] - 96), behavior: "smooth" });
      }
    };
    // страница уже нарисована — ищем сразу; иначе — когда появится её текстовый слой
    locate.current(target?.page ?? 0);
  }, [target, documentId, pageWidth]);
  // один и тот же обработчик для страницы на всё время жизни просмотрщика
  const [textLayerDone] = useState(() => {
    const handlers = new Map<number, () => void>();
    return (page: number) => {
      let handler = handlers.get(page);
      if (!handler) {
        handler = () => locate.current(page);
        handlers.set(page, handler);
      }
      return handler;
    };
  });

  // переход к цитате: страница → прямоугольник подсветки, с небольшим отступом сверху
  useEffect(() => {
    if (!target || target.documentId !== documentId || pages === 0) return;
    const node = pageRefs.current.get(target.page);
    const root = scroller.current;
    if (!node || !root) return;
    const box = target.highlights.find((h) => h.page === target.page)?.rects[0];
    const scale = pageWidth / (pointWidth[target.page] ?? pageWidth);
    const offset = box ? box[1] * scale : 0;
    root.scrollTo({ top: Math.max(0, node.offsetTop + offset - 96), behavior: "smooth" });
  }, [target, documentId, pages, pageWidth, pointWidth]);

  if (!documentId || !file) {
    return (
      <div style={{ height, display: "grid", placeItems: "center", background: brand.subtle }}>
        <Empty
          image={<FileSearchOutlined style={{ fontSize: 48, color: brand.grayLight }} />}
          description="Выберите замечание — здесь откроется документ с подсвеченной цитатой"
        />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height, background: "#E9EAEC" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          background: brand.surface,
          borderBottom: `1px solid ${brand.border}`,
        }}
      >
        <span style={{ fontWeight: 600, flex: "1 1 auto", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {filename}
        </span>
        {/* что показано — коротко, в строке инструментов: полоса над документом сдвигала
            текст и закрывала подсветку, а сама цитата и так выделена на странице */}
        {target?.caption && target.documentId === documentId && (
          <span className="viewer-caption" title={`Подсвечено в документе: ${target.caption}`} style={{ flex: "0 1 auto", minWidth: 0 }}>
            подсвечено: <b>{target.caption}</b>
          </span>
        )}
        {pages > 0 && (
          <span className="muted num" style={{ fontSize: 13 }}>
            стр. {current} из {pages}
          </span>
        )}
        <Tooltip title="Мельче">
          <Button size="small" icon={<MinusOutlined />} aria-label="Уменьшить" onClick={() => setZoom((z) => Math.max(0.6, z - 0.15))} />
        </Tooltip>
        <Tooltip title="Крупнее">
          <Button size="small" icon={<PlusOutlined />} aria-label="Увеличить" onClick={() => setZoom((z) => Math.min(2, z + 0.15))} />
        </Tooltip>
      </div>
      <div ref={scroller} style={{ flex: 1, overflow: "auto", padding: 16 }}>
        <Document
          file={file}
          loading={<Spin style={{ display: "block", margin: "48px auto" }} />}
          error={<Empty description="Не удалось открыть PDF документа" />}
          onLoadSuccess={async (pdf) => {
            setPages(pdf.numPages);
            const first = await pdf.getPage(1);
            const viewport = first.getViewport({ scale: 1 });
            setAspect(viewport.height / viewport.width);
          }}
        >
          {Array.from({ length: pages }, (_, i) => i + 1).map((page) => {
            const boxes = target && target.documentId === documentId
              ? target.highlights.filter((h) => h.page === page).flatMap((h) => h.rects)
              : [];
            const scale = pageWidth / (pointWidth[page] ?? pageWidth);
            const marks: Box[] = [
              ...boxes.map(([x0, y0, x1, y1]): Box => [x0 * scale, y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale]),
              ...(found && target && found.key === target.key && found.page === page ? found.boxes : []),
            ];
            const top = Math.min(...marks.map((m) => m[1]));
            const bottom = Math.max(...marks.map((m) => m[1] + m[3]));
            return (
              <div
                key={page}
                data-page={page}
                ref={(node) => {
                  if (node) pageRefs.current.set(page, node);
                  else pageRefs.current.delete(page);
                }}
                style={{
                  position: "relative",
                  width: pageWidth,
                  minHeight: Math.round(pageWidth * aspect),
                  margin: `0 auto ${PAGE_GAP}px`,
                  background: "#FFFFFF",
                  boxShadow: "0 1px 3px rgba(3,16,45,0.12)",
                }}
              >
                {near(page) ? (
                  <Page
                    pageNumber={page}
                    width={pageWidth}
                    renderAnnotationLayer={false}
                    renderTextLayer
                    loading={null}
                    onRenderTextLayerSuccess={textLayerDone(page)}
                    onLoadSuccess={(p) => setPointWidth((w) => (w[page] ? w : { ...w, [page]: p.originalWidth }))}
                  />
                ) : (
                  <div className="muted" style={{ padding: 16, fontSize: 12 }}>
                    Страница {page}
                  </div>
                )}
                {marks.map(([left, y, w, h], i) => (
                  <span
                    key={`${target?.key}-${i}`}
                    className="pdf-highlight active"
                    style={{ left: left - 2, top: y - 1, width: w + 4, height: h + 2 }}
                  />
                ))}
                {/* метка на полях: цитату видно, даже если она — одна строка на плотной странице */}
                {marks.length > 0 && (
                  <span
                    key={`marker-${target?.key}`}
                    className="pdf-highlight-marker"
                    style={{ top: top - 2, height: bottom - top + 4 }}
                    aria-hidden
                  />
                )}
              </div>
            );
          })}
        </Document>
      </div>
    </div>
  );
}

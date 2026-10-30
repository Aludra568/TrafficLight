/**
 * Куда вернуться после входа: только путь внутри сервиса. «//сайт» и «/\сайт» браузер и
 * React Router 6 понимают как другой адрес — ссылка на вход уводила бы на чужой сайт
 * (open redirect, GHSA-wrjc-x8rr-h8h6; обновление до React Router 7 — после сдачи).
 */
export function safeReturnPath(from: unknown): string {
  if (typeof from !== "string" || !from.startsWith("/")) return "/";
  // eslint-disable-next-line no-control-regex -- управляющие символы и есть то, что отсекаем
  if (/^\/[/\\]/.test(from) || /[\\\u0000-\u001f]/.test(from)) return "/";
  return from;
}

/**
 * Обращения к API. Ошибки сервера приходят как {error: {code, message}} — message уже по-русски
 * и показывается пользователю как есть. 401 — сессия истекла: токен стирается, приложение
 * отправляет на вход.
 */
export const TOKEN_KEY = "svetofor.token";
export const UNAUTHORIZED_EVENT = "svetofor:unauthorized";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// Хранилище браузера бывает недоступно (закрытый режим, запрет политикой) — тогда токен живёт
// в памяти до перезагрузки страницы, а не теряется сразу после входа. Пока хранилище работает,
// источник правды — оно: выход в соседней вкладке не оставляет здесь «застрявший» токен.
let memoryToken: string | null = null;
let storageWorks = true;

export const tokenStore = {
  get(): string | null {
    if (storageWorks) {
      try {
        return localStorage.getItem(TOKEN_KEY);
      } catch {
        storageWorks = false;
      }
    }
    return memoryToken;
  },
  set(token: string | null): void {
    memoryToken = token;
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      storageWorks = false; // дальше — только память
    }
  },
};

function headers(extra?: HeadersInit): Headers {
  const result = new Headers(extra);
  const token = tokenStore.get();
  if (token) result.set("Authorization", `Bearer ${token}`);
  return result;
}

async function failure(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = "Сервис временно недоступен. Попробуйте ещё раз";
  try {
    const body = await response.json();
    code = body?.error?.code ?? code;
    message = body?.error?.message ?? message;
  } catch {
    /* ответ не JSON — остаётся общее сообщение */
  }
  if (response.status === 401) {
    tokenStore.set(null);
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  return new ApiError(response.status, code, message);
}

type Options = Omit<RequestInit, "body"> & { json?: unknown; body?: BodyInit };

export async function request(path: string, options: Options = {}): Promise<Response> {
  const { json, headers: extra, ...init } = options;
  const finalHeaders = headers(extra);
  let body = options.body;
  if (json !== undefined) {
    finalHeaders.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  let response: Response;
  try {
    response = await fetch(path, { ...init, body, headers: finalHeaders });
  } catch {
    throw new ApiError(0, "network", "Нет связи с сервером. Проверьте подключение");
  }
  if (!response.ok) throw await failure(response);
  return response;
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const response = await request(path, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Имя файла из Content-Disposition (в том числе filename*=UTF-8''… с кириллицей). */
export function filenameFrom(response: Response, fallback: string): string {
  const header = response.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) return decodeURIComponent(encoded[1]);
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : fallback;
}

/** Скачать файл с авторизацией: fetch → Blob → ссылка-однодневка. */
export async function download(path: string, fallback: string, options: Options = {}): Promise<void> {
  await saveResponse(await request(path, options), fallback);
}

export async function saveResponse(response: Response, fallback: string): Promise<void> {
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filenameFrom(response, fallback);
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

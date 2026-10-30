import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api, TOKEN_KEY, tokenStore, UNAUTHORIZED_EVENT } from "../api/client";
import { keys } from "../api/hooks";
import type { TokenOut, User } from "../api/types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<User>;
  logout: () => void;
  can: (role: User["role"]) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);
const LEVEL: Record<User["role"], number> = { specialist: 1, methodologist: 2, admin: 3 };

/**
 * Токен — состояние React, а не только запись в хранилище: вход и выход сразу перерисовывают
 * приложение. (Раньше пользователь вычислялся из хранилища при рендере, и TanStack Query,
 * отслеживающий только прочитанные поля, не сообщал о данных, которые до входа никто не читал, —
 * после успешного входа приложение оставалось на странице входа.)
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [token, setToken] = useState<string | null>(() => tokenStore.get());
  const me = useQuery({
    queryKey: keys.me, // сам токен в ключ не кладём: ключи видны в инструментах отладки
    queryFn: () => api<User>("/api/auth/me"),
    enabled: token !== null,
    retry: false,
    staleTime: Infinity,
  });

  const logout = useCallback(() => {
    tokenStore.set(null);
    setToken(null);
    client.clear();
  }, [client]);

  useEffect(() => {
    // сессия истекла (401 на любой запрос) — на вход
    const onUnauthorized = () => {
      client.removeQueries({ queryKey: keys.me });
      setToken(null);
    };
    // вход и выход в соседней вкладке применяются и здесь
    const onStorage = (event: StorageEvent) => {
      if (event.key !== TOKEN_KEY && event.key !== null) return;
      client.removeQueries({ queryKey: keys.me }); // другой пользователь — другой профиль
      setToken(tokenStore.get());
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
      window.removeEventListener("storage", onStorage);
    };
  }, [client]);

  const login = useCallback(
    async (email: string, password: string) => {
      const answer = await api<TokenOut>("/api/auth/login", {
        method: "POST",
        json: { email, password },
      });
      tokenStore.set(answer.access_token);
      // профиль пришёл вместе с токеном — лишний запрос /me не нужен
      client.setQueryData(keys.me, answer.user);
      setToken(answer.access_token);
      return answer.user;
    },
    [client],
  );

  // поля результата читаются всегда, без условий: TanStack Query следит только за прочитанными
  const { data, isLoading } = me;
  const user = token ? (data ?? null) : null;
  const loading = token !== null && isLoading;
  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      login,
      logout,
      can: (role) => Boolean(user && LEVEL[user.role] >= LEVEL[role]),
    }),
    [user, loading, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth вне AuthProvider");
  return value;
}

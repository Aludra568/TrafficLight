import { useEffect, useState } from "react";

/** Значение «догоняет» исходное через `delay` мс тишины: поиск не дёргает сервер на каждую букву. */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

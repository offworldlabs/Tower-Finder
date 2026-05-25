import { useEffect, useRef, useState } from "react";

/**
 * `useState` whose value is mirrored to `localStorage` under `key`.
 *
 * On mount, the initial value is read from storage if present; otherwise
 * `initial` is used.  On every change the new value is JSON-serialised
 * back to storage.  Storage errors (private browsing, quota) are
 * swallowed so a broken storage layer never crashes the map.
 *
 * Usage mirrors `useState` exactly:
 *     const [showTrails, setShowTrails] = usePersistedState("tf.trails", true);
 */
export function usePersistedState<T>(key: string, initial: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = typeof window !== "undefined" ? window.localStorage.getItem(key) : null;
      if (raw == null) return initial;
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });

  // Skip the first write — we just read this value, no point round-tripping
  // it back to storage and waking up other tabs.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    try {
      if (typeof window === "undefined") return;
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* quota exceeded / private mode — ignore */
    }
  }, [key, value]);

  return [value, setValue];
}

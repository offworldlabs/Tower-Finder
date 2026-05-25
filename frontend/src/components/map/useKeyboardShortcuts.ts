import { useEffect } from "react";

export type ShortcutMap = Record<string, (e: KeyboardEvent) => void>;

/**
 * Wire up a small set of single-key shortcuts.  Ignores events fired while
 * an input/textarea is focused so typing into the search box doesn't
 * accidentally toggle the trail layer.
 */
export function useKeyboardShortcuts(map: ShortcutMap, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) {
          // Allow Escape to bubble even from inputs so users can close the
          // search box with the keyboard.
          if (e.key !== "Escape") return;
        }
      }
      // Ignore when a modifier is pressed — we don't want to intercept
      // browser shortcuts (Cmd+R, Ctrl+L, …).
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const handler = map[e.key] || map[e.key.toLowerCase()];
      if (handler) {
        handler(e);
        e.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [map, enabled]);
}

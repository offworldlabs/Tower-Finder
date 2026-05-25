/**
 * Tiny toast system — no library, no provider. A singleton mounts itself
 * lazily into `document.body` the first time `toast()` is called and
 * renders a stack of dismissible chips at the bottom-right.
 *
 * Usage: `toast("Copied")`, `toast("Failed", { tone: "error" })`.
 */

type Tone = "info" | "success" | "error" | "warn";

interface ToastEntry {
  id: number;
  text: string;
  tone: Tone;
}

let _container: HTMLDivElement | null = null;
let _entries: ToastEntry[] = [];
let _idSeq = 1;

const TONE_BG: Record<Tone, string> = {
  info: "#1e293b",
  success: "#065f46",
  error: "#7f1d1d",
  warn: "#78350f",
};

const TONE_BORDER: Record<Tone, string> = {
  info: "#334155",
  success: "#10b981",
  error: "#f43f5e",
  warn: "#f59e0b",
};

function ensureContainer(): HTMLDivElement {
  if (_container && document.body.contains(_container)) return _container;
  const c = document.createElement("div");
  c.setAttribute("data-tf-toast", "");
  Object.assign(c.style, {
    position: "fixed",
    bottom: "16px",
    right: "16px",
    zIndex: "2000",
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    pointerEvents: "none",
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
  } as CSSStyleDeclaration);
  document.body.appendChild(c);
  _container = c;
  return c;
}

function render() {
  const c = ensureContainer();
  c.innerHTML = "";
  for (const e of _entries) {
    const el = document.createElement("div");
    el.textContent = e.text;
    Object.assign(el.style, {
      background: TONE_BG[e.tone],
      border: `1px solid ${TONE_BORDER[e.tone]}`,
      color: "#e2e8f0",
      padding: "8px 12px",
      borderRadius: "6px",
      fontSize: "13px",
      boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
      pointerEvents: "auto",
      maxWidth: "320px",
    } as CSSStyleDeclaration);
    c.appendChild(el);
  }
}

export function toast(text: string, opts: { tone?: Tone; durationMs?: number } = {}) {
  if (typeof window === "undefined") return;
  const id = _idSeq++;
  const entry: ToastEntry = { id, text, tone: opts.tone ?? "info" };
  _entries.push(entry);
  render();
  const duration = opts.durationMs ?? 2500;
  window.setTimeout(() => {
    _entries = _entries.filter((e) => e.id !== id);
    render();
  }, duration);
}

/** Copy a string to the clipboard, with a built-in success/failure toast. */
export async function copyToClipboard(text: string, label = "Copied"): Promise<boolean> {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for non-secure contexts
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    toast(label, { tone: "success" });
    return true;
  } catch {
    toast("Copy failed", { tone: "error" });
    return false;
  }
}

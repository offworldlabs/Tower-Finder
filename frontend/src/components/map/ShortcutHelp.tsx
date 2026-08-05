interface ShortcutHelpProps {
  visible: boolean;
  onClose: () => void;
}

// Keep in sync with `shortcutMap` in LiveAircraftMap.tsx — this table is the
// user-facing documentation of those bindings.  (It previously listed an "O"
// binding that never existed and omitted Shift+X / P / M / N.)
const SHORTCUTS: Array<[string, string]> = [
  ["?",       "Show / hide this help"],
  ["/",       "Focus the aircraft search box"],
  ["Escape",  "Clear search / deselect aircraft"],
  ["Space",   "Pause / resume live feed"],
  ["F",       "Toggle filter panel"],
  ["L",       "Toggle aircraft labels"],
  ["T",       "Toggle trails"],
  ["C",       "Toggle node coverage zones"],
  ["I",       "Toggle illuminators (TX towers)"],
  ["G",       "Toggle ground-truth overlay"],
  ["A",       "Colour aircraft by altitude"],
  ["R",       "Range rings around selected"],
  ["S",       "Live stats panel"],
  ["X",       "Export selected aircraft trail (CSV)"],
  ["Shift+X", "Export all aircraft trails (CSV)"],
  ["P",       "Pin / unpin selected aircraft"],
  ["M",       "Locate me"],
  ["N",       "Toggle emergency-squawk sound"],
];

export default function ShortcutHelp({ visible, onClose }: ShortcutHelpProps) {
  if (!visible) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: "absolute",
        inset: 0,
        background: "rgba(2, 6, 23, 0.55)",
        zIndex: 1500,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "#020617",
          border: "1px solid #1e293b",
          borderRadius: 12,
          padding: "20px 24px",
          color: "#e2e8f0",
          fontSize: 13,
          minWidth: 340,
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.5)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <strong style={{ fontSize: 15 }}>Keyboard shortcuts</strong>
          <button
            onClick={onClose}
            style={{ background: "none", border: "none", color: "#94a3b8", fontSize: 18, cursor: "pointer", lineHeight: 1 }}
            title="Close"
          >
            ×
          </button>
        </div>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <tbody>
            {SHORTCUTS.map(([key, label]) => (
              <tr key={key}>
                <td style={{ padding: "4px 12px 4px 0", verticalAlign: "top" }}>
                  <kbd
                    style={{
                      background: "#1e293b",
                      border: "1px solid #334155",
                      borderRadius: 4,
                      padding: "2px 8px",
                      fontFamily: "ui-monospace, SFMono-Regular, monospace",
                      fontSize: 12,
                      color: "#e2e8f0",
                      minWidth: 28,
                      display: "inline-block",
                      textAlign: "center",
                    }}
                  >
                    {key}
                  </kbd>
                </td>
                <td style={{ padding: "4px 0", color: "#cbd5e1" }}>{label}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ marginTop: 12, color: "#64748b", fontSize: 11 }}>
          Shortcuts are ignored while typing in the search box.
        </div>
      </div>
    </div>
  );
}

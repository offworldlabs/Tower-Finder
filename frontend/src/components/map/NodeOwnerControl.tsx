// @ts-nocheck — gradual TS migration
import { useState } from "react";
import { API_BASE } from "./constants";

/* ── NodeOwnerControl: top-right map overlay for the node-owner view.
      - Logged out: a "Sign in" button that reveals Google/GitHub OAuth links
        (redirecting back to the current map URL).
      - Logged in: the user's name plus a "My nodes only" toggle that filters
        the map to a server-authenticated feed of just their own nodes. ── */

const boxStyle = {
  background: "rgba(2, 6, 23, 0.88)",
  color: "#e2e8f0",
  border: "1px solid #1e293b",
  borderRadius: 8,
  padding: "8px 10px",
  fontFamily: "ui-sans-serif, system-ui, sans-serif",
  fontSize: 12,
  boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
  maxWidth: 240,
};

const linkBtnStyle = {
  display: "block",
  width: "100%",
  textAlign: "center",
  padding: "6px 10px",
  marginTop: 6,
  background: "#1e293b",
  color: "#e2e8f0",
  border: "1px solid #334155",
  borderRadius: 6,
  textDecoration: "none",
  cursor: "pointer",
};

export default function NodeOwnerControl({ user, ownedCount, ownerOnly, onToggle, loading }) {
  const [menuOpen, setMenuOpen] = useState(false);

  if (loading) return null;

  const redirect = encodeURIComponent(window.location.href);

  if (!user) {
    return (
      <div style={boxStyle}>
        <button
          onClick={() => setMenuOpen((v) => !v)}
          style={{ ...linkBtnStyle, marginTop: 0, background: "#0ea5e9", borderColor: "#0ea5e9", color: "#04293a", fontWeight: 600 }}
        >
          Sign in to see your node
        </button>
        {menuOpen && (
          <>
            <a style={linkBtnStyle} href={`${API_BASE}/auth/login/google?redirect=${redirect}`}>
              Continue with Google
            </a>
            <a style={linkBtnStyle} href={`${API_BASE}/auth/login/github?redirect=${redirect}`}>
              Continue with GitHub
            </a>
          </>
        )}
      </div>
    );
  }

  return (
    <div style={boxStyle}>
      <div style={{ color: "#94a3b8", marginBottom: 6 }}>
        {user.name || user.email}
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
        <input type="checkbox" checked={ownerOnly} onChange={(e) => onToggle(e.target.checked)} />
        My nodes only
      </label>
      {ownerOnly && ownedCount === 0 && (
        <div style={{ color: "#f59e0b", marginTop: 6, lineHeight: 1.4 }}>
          No nodes linked to your account yet. Claim a node from your dashboard.
        </div>
      )}
    </div>
  );
}

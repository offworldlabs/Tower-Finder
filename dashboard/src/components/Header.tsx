import { useState, useRef, useEffect } from "react";
import { useAuth } from "../context/AuthContext";
import { useNavigate } from "react-router-dom";

export default function Header({ title }) {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  return (
    <header className="header">
      <div className="header-title">{title}</div>
      <div className="header-actions">
        <div className="header-user" ref={ref} onClick={() => setOpen(!open)}>
          {user?.avatar ? (
            <img src={user.avatar} alt="" referrerPolicy="no-referrer" />
          ) : (
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: "50%",
                background: "var(--accent)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              {(user?.name || "U")[0].toUpperCase()}
            </div>
          )}
          <span className="user-name">{user?.name}</span>
          {open && (
            <div className="user-dropdown">
              <button disabled style={{ color: "var(--text-muted)", fontSize: 11 }}>
                {user?.email}
              </button>
              <button onClick={handleLogout}>Sign out</button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

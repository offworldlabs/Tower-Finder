import { createContext, useContext, useState, useEffect } from "react";
import { api } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function fetchUser() {
      for (let attempt = 0; attempt < 4; attempt++) {
        try {
          const u = await api.me();
          if (!cancelled) { setUser(u); setLoading(false); }
          return;
        } catch (e) {
          // client.js already redirects to /login on 401 — here we only
          // land when there's a network/timeout error (server busy).
          if (attempt < 3) {
            await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
          }
        }
      }
      if (!cancelled) { setUser(null); setLoading(false); }
    }

    fetchUser();
    return () => { cancelled = true; };
  }, []);

  const logout = async () => {
    await api.logout();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}

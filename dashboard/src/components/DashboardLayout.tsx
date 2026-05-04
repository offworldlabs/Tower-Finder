import { useLocation } from "react-router-dom";
import Sidebar from "./Sidebar";
import Header from "./Header";

const pageTitles = {
  "/": { user: "Overview", admin: "Network Health" },
  "/detections": { user: "Detections" },
  "/rf": { user: "RF Environment" },
  "/contribution": { user: "Network Contribution" },
  "/data": { user: "Data Explorer" },
  "/alerts": { user: "Alerts & Notifications" },
  "/anomalies": { user: "Anomaly Monitor", admin: "Anomaly Monitor" },
  "/leaderboard": { user: "Leaderboard" },
  "/knowledge": { user: "Knowledge Base" },
  "/tunnel": { user: "Tunnel & Local Display" },
  "/onboarding": { user: "My Nodes" },
  "/settings": { user: "Settings" },
  "/nodes": { admin: "Node Management" },
  "/analytics": { admin: "Analytics" },
  "/events": { admin: "Events & Alerts" },
  "/storage": { admin: "Data & Storage" },
  "/system": { admin: "System Metrics" },
  "/invites": { admin: "Invites" },
  "/custody": { admin: "Chain of Custody" },
  "/users": { admin: "User Management" },
  "/config": { admin: "Configuration" },
};

export default function DashboardLayout({ isAdmin, children }) {
  const { pathname } = useLocation();
  const mode = isAdmin ? "admin" : "user";
  const segments = pathname.split("/").filter(Boolean);
  const basePath = segments.length ? `/${segments[0]}` : "/";
  const entry = pageTitles[basePath];
  const title = entry?.[mode] || (pathname.includes("/nodes/") ? "Node Detail" : "Dashboard");

  return (
    <div className="dashboard">
      <Sidebar isAdmin={isAdmin} />
      <div className="main-area">
        <Header title={title} />
        <div className="content">{children}</div>
      </div>
    </div>
  );
}

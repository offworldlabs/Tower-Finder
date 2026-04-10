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
  "/leaderboard": { user: "Leaderboard" },
  "/knowledge": { user: "Knowledge Base" },
  "/tunnel": { user: "Tunnel & Local Display" },
  "/settings": { user: "Settings" },
  "/nodes": { admin: "Node Management" },
  "/analytics": { admin: "Analytics" },
  "/events": { admin: "Events & Alerts" },
  "/storage": { admin: "Data & Storage" },
  "/custody": { admin: "Chain of Custody" },
  "/users": { admin: "User Management" },
  "/config": { admin: "Configuration" },
};

export default function DashboardLayout({ isAdmin, children }) {
  const { pathname } = useLocation();
  const mode = isAdmin ? "admin" : "user";
  const basePath = "/" + pathname.split("/").filter(Boolean)[0] || "/";
  const entry = pageTitles[basePath === "/" ? "/" : `/${pathname.split("/").filter(Boolean)[0]}`];
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

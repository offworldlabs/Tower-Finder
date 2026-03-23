import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./context/AuthContext";
import LoginPage from "./pages/LoginPage";
import DashboardLayout from "./components/DashboardLayout";

// User pages
import OverviewPage from "./pages/user/OverviewPage";
import NodeDetailPage from "./pages/user/NodeDetailPage";
import DetectionsPage from "./pages/user/DetectionsPage";
import ContributionPage from "./pages/user/ContributionPage";
import DataExplorerPage from "./pages/user/DataExplorerPage";
import SettingsPage from "./pages/user/SettingsPage";
import RFEnvironmentPage from "./pages/user/RFEnvironmentPage";
import AlertsPage from "./pages/user/AlertsPage";
import LeaderboardPage from "./pages/user/LeaderboardPage";
import KnowledgeBasePage from "./pages/user/KnowledgeBasePage";
import TunnelLinkPage from "./pages/user/TunnelLinkPage";

// Admin pages
import NetworkHealthPage from "./pages/admin/NetworkHealthPage";
import NodeManagementPage from "./pages/admin/NodeManagementPage";
import AnalyticsPage from "./pages/admin/AnalyticsPage";
import EventsPage from "./pages/admin/EventsPage";
import StoragePage from "./pages/admin/StoragePage";
import CustodyPage from "./pages/admin/CustodyPage";
import UserManagementPage from "./pages/admin/UserManagementPage";
import ConfigPage from "./pages/admin/ConfigPage";

const isAdminSite =
  window.location.hostname.startsWith("admin.") ||
  new URLSearchParams(window.location.search).get("mode") === "admin";

function RequireAuth({ children }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="loading-screen">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (isAdminSite && user.role !== "admin") {
    return (
      <div className="access-denied">
        <h2>Access Denied</h2>
        <p>Admin privileges required.</p>
      </div>
    );
  }
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/*"
        element={
          <RequireAuth>
            <DashboardLayout isAdmin={isAdminSite}>
              <Routes>
                {isAdminSite ? (
                  <>
                    <Route index element={<NetworkHealthPage />} />
                    <Route path="nodes" element={<NodeManagementPage />} />
                    <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
                    <Route path="analytics" element={<AnalyticsPage />} />
                    <Route path="events" element={<EventsPage />} />
                    <Route path="storage" element={<StoragePage />} />
                    <Route path="custody" element={<CustodyPage />} />
                    <Route path="users" element={<UserManagementPage />} />
                    <Route path="config" element={<ConfigPage />} />
                  </>
                ) : (
                  <>
                    <Route index element={<OverviewPage />} />
                    <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
                    <Route path="detections" element={<DetectionsPage />} />
                    <Route path="rf" element={<RFEnvironmentPage />} />
                    <Route path="contribution" element={<ContributionPage />} />
                    <Route path="data" element={<DataExplorerPage />} />
                    <Route path="alerts" element={<AlertsPage />} />
                    <Route path="leaderboard" element={<LeaderboardPage />} />
                    <Route path="knowledge" element={<KnowledgeBasePage />} />
                    <Route path="tunnel" element={<TunnelLinkPage />} />
                    <Route path="settings" element={<SettingsPage />} />
                  </>
                )}
              </Routes>
            </DashboardLayout>
          </RequireAuth>
        }
      />
    </Routes>
  );
}

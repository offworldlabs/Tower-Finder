import { lazy, Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./context/AuthContext";
import LoginPage from "./pages/LoginPage";
import DashboardLayout from "./components/DashboardLayout";

// User pages — lazy-loaded so each chunk is only downloaded when first visited
const OverviewPage = lazy(() => import("./pages/user/OverviewPage"));
const NodeDetailPage = lazy(() => import("./pages/user/NodeDetailPage"));
const DetectionsPage = lazy(() => import("./pages/user/DetectionsPage"));
const ContributionPage = lazy(() => import("./pages/user/ContributionPage"));
const DataExplorerPage = lazy(() => import("./pages/user/DataExplorerPage"));
const SettingsPage = lazy(() => import("./pages/user/SettingsPage"));
const RFEnvironmentPage = lazy(() => import("./pages/user/RFEnvironmentPage"));
const AlertsPage = lazy(() => import("./pages/user/AlertsPage"));
const LeaderboardPage = lazy(() => import("./pages/user/LeaderboardPage"));
const KnowledgeBasePage = lazy(() => import("./pages/user/KnowledgeBasePage"));
const TunnelLinkPage = lazy(() => import("./pages/user/TunnelLinkPage"));
const AnomalyPage = lazy(() => import("./pages/user/AnomalyPage"));
const OnboardingPage = lazy(() => import("./pages/user/OnboardingPage"));

// Admin pages — lazy-loaded
const NetworkHealthPage = lazy(() => import("./pages/admin/NetworkHealthPage"));
const NodeManagementPage = lazy(() => import("./pages/admin/NodeManagementPage"));
const AnalyticsPage = lazy(() => import("./pages/admin/AnalyticsPage"));
const EventsPage = lazy(() => import("./pages/admin/EventsPage"));
const StoragePage = lazy(() => import("./pages/admin/StoragePage"));
const CustodyPage = lazy(() => import("./pages/admin/CustodyPage"));
const UserManagementPage = lazy(() => import("./pages/admin/UserManagementPage"));
const InvitesPage = lazy(() => import("./pages/admin/InvitesPage"));
const ConfigPage = lazy(() => import("./pages/admin/ConfigPage"));
const SystemMetricsPage = lazy(() => import("./pages/admin/SystemMetricsPage"));
const MlatVerificationPage = lazy(() => import("./pages/admin/MlatVerificationPage"));

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
              <Suspense fallback={<div className="loading-screen">Loading…</div>}>
                <Routes>
                  {isAdminSite ? (
                    <>
                      <Route index element={<NetworkHealthPage />} />
                      <Route path="nodes" element={<NodeManagementPage />} />
                      <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
                      <Route path="analytics" element={<AnalyticsPage />} />
                      <Route path="mlat" element={<MlatVerificationPage />} />
                      <Route path="anomalies" element={<AnomalyPage />} />
                      <Route path="events" element={<EventsPage />} />
                      <Route path="storage" element={<StoragePage />} />
                      <Route path="custody" element={<CustodyPage />} />
                      <Route path="users" element={<UserManagementPage />} />
                      <Route path="invites" element={<InvitesPage />} />
                      <Route path="config" element={<ConfigPage />} />
                      <Route path="system" element={<SystemMetricsPage />} />
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
                      <Route path="anomalies" element={<AnomalyPage />} />
                      <Route path="leaderboard" element={<LeaderboardPage />} />
                      <Route path="knowledge" element={<KnowledgeBasePage />} />
                      <Route path="tunnel" element={<TunnelLinkPage />} />
                      <Route path="onboarding" element={<OnboardingPage />} />
                      <Route path="settings" element={<SettingsPage />} />
                    </>
                  )}
                </Routes>
              </Suspense>
            </DashboardLayout>
          </RequireAuth>
        }
      />
    </Routes>
  );
}

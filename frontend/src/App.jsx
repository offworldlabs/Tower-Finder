import { useState, lazy, Suspense } from "react";
import SearchForm from "./components/SearchForm";
import ResultsTable from "./components/ResultsTable";
import TowerMap from "./components/TowerMap";
import PhysicsSettings from "./components/PhysicsSettings";

// Leaflet is ~300 KB — only load it when the Live Radar tab is first opened
const LiveAircraftMap = lazy(() => import("./components/LiveAircraftMap"));
import { fetchTowers } from "./api";

function SummaryStrip({ towers }) {
  if (!towers.length) return null;

  const ideal = towers.filter((t) => t.distance_class === "Ideal").length;
  const bands = [...new Set(towers.map((t) => t.band))];
  const best = towers[0];

  return (
    <div className="summary-strip">
      <div className="stat-card">
        <span className="stat-value">{towers.length}</span>
        <span className="stat-label">Towers Found</span>
      </div>
      <div className="stat-card">
        <span className="stat-value">{ideal}</span>
        <span className="stat-label">Ideal Range</span>
      </div>
      <div className="stat-card">
        <span className="stat-value">{bands.join(", ")}</span>
        <span className="stat-label">Bands</span>
      </div>
      {best && (
        <div className="stat-card">
          <span className="stat-value">{best.callsign || "—"}</span>
          <span className="stat-label">Top Pick — {best.distance_km} km</span>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [towers, setTowers] = useState([]);
  const [query, setQuery] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [highlighted, setHighlighted] = useState(null);

  // On map.retina.fm / testmap.retina.fm, default to "live" and hide tower search
  const isMapDomain = /^(test)?map\./i.test(window.location.hostname);
  const [activeTab, setActiveTab] = useState(isMapDomain ? "live" : "towers");

  async function handleSearch({ lat, lon, altitude, source, frequencies }) {
    setLoading(true);
    setError(null);
    setTowers([]);
    setQuery(null);

    try {
      const data = await fetchTowers(lat, lon, altitude, 20, source, frequencies || []);
      setTowers(data.towers);
      setQuery(data.query);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  // Keep the map mounted once it has been opened so it doesn't re-initialise
  const [liveEverOpened, setLiveEverOpened] = useState(isMapDomain);

  return (
    <div className="app">
      <header className="app-header">
        <span className="header-icon">&#9041;</span>
        <h1>{isMapDomain ? "RETINA" : "Tower Finder"}</h1>
        <span className="subtitle">{isMapDomain ? "Passive Radar Live Map" : "Passive Radar Illuminator Search"}</span>
        <nav className="header-tabs">
          {!isMapDomain && (
            <button
              className={`tab-btn ${activeTab === "towers" ? "active" : ""}`}
              onClick={() => setActiveTab("towers")}
            >
              Tower Search
            </button>
          )}
          <button
            className={`tab-btn ${activeTab === "physics" ? "active" : ""}`}
            onClick={() => setActiveTab("physics")}
          >
            Physics Layer
          </button>
          <button
            className={`tab-btn ${activeTab === "live" ? "active" : ""}`}
            onClick={() => { setActiveTab("live"); setLiveEverOpened(true); }}
          >
            Live Radar
          </button>
        </nav>
      </header>

      <main className={`app-body${activeTab === "live" ? " live-active" : ""}${activeTab === "physics" ? " physics-active" : ""}`}>
        {activeTab === "towers" && (
          <>
            <div className="top-section">
              <SearchForm onSearch={handleSearch} loading={loading} />
              <TowerMap
                towers={towers}
                userLocation={query}
                highlighted={highlighted}
              />
            </div>

            {error && <div className="error-banner">{error}</div>}

            {loading && (
              <div className="loading-section">
                <div className="spinner" />
                <div className="loading-bar">
                  <div className="loading-bar-inner" />
                </div>
                <p className="loading-text">
                  Querying broadcast licence database — this may take up to a minute…
                </p>
              </div>
            )}

            <SummaryStrip towers={towers} />

            {towers.length > 0 && (
              <ResultsTable
                towers={towers}
                onHover={setHighlighted}
              />
            )}

            {!loading && query && towers.length === 0 && (
              <p className="no-results">
                No suitable broadcast towers found within 80 km.
              </p>
            )}
          </>
        )}

        {/* Live map: mounted once ever, hidden when inactive to preserve WebSocket state */}
        <Suspense fallback={<div style={{display:"flex",alignItems:"center",justifyContent:"center",height:"100%",color:"#94a3b8",fontSize:"0.9rem"}}>Loading map…</div>}>
          <div style={{ display: activeTab === "live" ? "contents" : "none" }}>
            {liveEverOpened && <LiveAircraftMap />}
          </div>
        </Suspense>
        {activeTab === "physics" && <PhysicsSettings />}
      </main>
    </div>
  );
}

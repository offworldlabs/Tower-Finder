import "./ResultsTable.css";

const CLASS_COLORS = {
  Ideal: "#16a34a",
  Good: "#ca8a04",
  Far: "#6b7280",
  "Too Close": "#dc2626",
};

const CLASS_BG = {
  Ideal: "rgba(22, 163, 74, 0.08)",
  Good: "rgba(202, 138, 4, 0.08)",
  Far: "rgba(107, 114, 128, 0.08)",
  "Too Close": "rgba(220, 38, 38, 0.08)",
};

const BAND_COLORS = {
  VHF: "#7c3aed",
  UHF: "#0891b2",
  FM: "#db2777",
};

const BAND_BG = {
  VHF: "rgba(124, 58, 237, 0.08)",
  UHF: "rgba(8, 145, 178, 0.08)",
  FM: "rgba(219, 39, 119, 0.08)",
};

export default function ResultsTable({ towers, onHover }) {
  return (
    <div className="results-wrap">
      <h2>Results <span className="results-count">{towers.length}</span></h2>
      <div className="table-scroll">
        <table className="results-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Callsign</th>
              <th>Location</th>
              <th>Lat</th>
              <th>Long</th>
              <th>Altitude (m)</th>
              <th>Ant. Height (m)</th>
              <th>Freq (MHz)</th>
              <th>Band</th>
              <th>EIRP</th>
              <th>Distance</th>
              <th>Bearing</th>
              <th>Rx Power</th>
              <th>Suitability</th>
            </tr>
          </thead>
          <tbody>
            {towers.map((t) => (
              <tr
                key={`${t.callsign}-${t.frequency_mhz}`}
                onMouseEnter={() => onHover(t)}
                onMouseLeave={() => onHover(null)}
              >
                <td className="rank">{t.rank}</td>
                <td className="callsign">{t.callsign || "—"}</td>
                <td className="location-name" title={`${t.name}${t.state ? `, ${t.state}` : ""}`}>
                  {t.name}
                  {t.state ? `, ${t.state}` : ""}
                </td>
                <td className="mono">{t.latitude}</td>
                <td className="mono">{t.longitude}</td>
                <td className="mono">{t.altitude_m != null ? t.altitude_m : "—"}</td>
                <td className="mono">{t.antenna_height_m != null ? t.antenna_height_m : "—"}</td>
                <td className="mono">
                  {t.frequency_mhz}
                  {t.frequency_matched && (
                    <span className="freq-match-badge" title="Matches measured frequency">&#10003;</span>
                  )}
                </td>
                <td>
                  <span
                    className="badge"
                    style={{
                      color: BAND_COLORS[t.band] || "#6b7280",
                      background: BAND_BG[t.band] || "rgba(107,114,128,0.08)",
                    }}
                  >
                    {t.band}
                  </span>
                </td>
                <td className="mono">{t.eirp_dbm} dBm</td>
                <td className="mono">{t.distance_km} km</td>
                <td>
                  {t.bearing_deg}° <span className="cardinal">{t.bearing_cardinal}</span>
                </td>
                <td className="mono power">{t.received_power_dbm} dBm</td>
                <td>
                  <span
                    className="badge"
                    style={{
                      color: CLASS_COLORS[t.distance_class] || "#6b7280",
                      background: CLASS_BG[t.distance_class] || "rgba(107,114,128,0.08)",
                    }}
                  >
                    {t.distance_class}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

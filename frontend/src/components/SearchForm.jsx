import { useState, useEffect, useRef } from "react";
import { fetchElevation } from "../api";
import "./SearchForm.css";

/**
 * Detect the most likely data source based on lat/lon bounding boxes.
 * Returns "au", "us", "ca", or null if unknown.
 */
function detectSource(lat, lon) {
  if (lat == null || lon == null || isNaN(lat) || isNaN(lon)) return null;
  // Australia
  if (lat >= -45 && lat <= -10 && lon >= 112 && lon <= 155) return "au";
  // Canada — checked before US to include southern Ontario/Quebec (down to 42°N)
  if (lat >= 42 && lat <= 84 && lon >= -141 && lon <= -52) return "ca";
  // United States (continental + Alaska + Hawaii)
  if (lat >= 24 && lat < 49 && lon >= -125 && lon <= -66) return "us";
  // Alaska
  if (lat >= 51 && lat <= 72 && lon >= -180 && lon <= -129) return "us";
  // Hawaii
  if (lat >= 18 && lat <= 23 && lon >= -161 && lon <= -154) return "us";
  return null;
}

export default function SearchForm({ onSearch, loading }) {
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [altitude, setAltitude] = useState("");
  const [source, setSource] = useState("us");
  const [geoError, setGeoError] = useState(null);
  const altitudeManual = useRef(false);
  const [frequencies, setFrequencies] = useState([""]);
  const [showFrequencies, setShowFrequencies] = useState(false);

  // Auto-detect data source when coordinates change
  useEffect(() => {
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    const detected = detectSource(parsedLat, parsedLon);
    if (detected) setSource(detected);
  }, [lat, lon]);

  // Auto-lookup elevation when lat/lon change and altitude hasn't been manually set
  useEffect(() => {
    if (altitudeManual.current) return;
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;

    let cancelled = false;
    fetchElevation(parsedLat, parsedLon).then((elev) => {
      if (!cancelled && elev != null && !altitudeManual.current) {
        setAltitude(Math.round(elev).toString());
      }
    });
    return () => { cancelled = true; };
  }, [lat, lon]);

  function handleSubmit(e) {
    e.preventDefault();
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;
    const parsedFreqs = frequencies
      .map((f) => parseFloat(f))
      .filter((f) => !isNaN(f) && f > 0);
    onSearch({
      lat: parsedLat,
      lon: parsedLon,
      altitude: parseFloat(altitude) || 0,
      source,
      frequencies: parsedFreqs,
    });
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      setGeoError("Geolocation not supported by your browser");
      return;
    }
    setGeoError(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setLat(pos.coords.latitude.toFixed(6));
        setLon(pos.coords.longitude.toFixed(6));
        if (pos.coords.altitude != null) {
          setAltitude(Math.round(pos.coords.altitude).toString());
        }
      },
      (err) => {
        const msgs = {
          1: "Location access denied — please allow location in browser settings",
          2: "Location unavailable",
          3: "Location request timed out",
        };
        setGeoError(msgs[err.code] || err.message);
      },
      { timeout: 10000, maximumAge: 60000, enableHighAccuracy: false }
    );
  }

  return (
    <form className="search-form" onSubmit={handleSubmit}>
      <h2>Location</h2>

      <div className="field-row">
        <label>
          Latitude
          <input
            type="number"
            step="any"
            min={-90}
            max={90}
            value={lat}
            onChange={(e) => setLat(e.target.value)}
            placeholder="e.g. 38.8977"
            required
          />
        </label>
        <label>
          Longitude
          <input
            type="number"
            step="any"
            min={-180}
            max={180}
            value={lon}
            onChange={(e) => setLon(e.target.value)}
            placeholder="e.g. -77.0365"
            required
          />
        </label>
      </div>

      <div className="field-row">
        <label>
          Altitude (m)
          <input
            type="number"
            step="any"
            min={0}
            value={altitude}
            onChange={(e) => {
              setAltitude(e.target.value);
              if (e.target.value !== "") altitudeManual.current = true;
              else altitudeManual.current = false;
            }}
            placeholder="Auto-detected"
          />
        </label>
        <label>
          Data Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="us">United States (FCC)</option>
            <option value="ca">Canada (ISED)</option>
            <option value="au">Australia (ACMA)</option>
          </select>
        </label>
      </div>

      <div className="freq-toggle">
        <button
          type="button"
          className="btn-link"
          onClick={() => setShowFrequencies(!showFrequencies)}
        >
          {showFrequencies ? "Hide" : "Add"} Measured Frequencies
        </button>
      </div>

      {showFrequencies && (
        <div className="freq-section">
          <label className="freq-label">Measured Frequencies (MHz)</label>
          <div className="freq-inputs">
            {frequencies.map((freq, i) => (
              <div key={i} className="freq-row">
                <input
                  type="number"
                  step="any"
                  min={0}
                  value={freq}
                  onChange={(e) => {
                    const updated = [...frequencies];
                    updated[i] = e.target.value;
                    setFrequencies(updated);
                  }}
                  placeholder={`Freq ${i + 1} (MHz)`}
                />
                {frequencies.length > 1 && (
                  <button
                    type="button"
                    className="btn-remove-freq"
                    onClick={() => setFrequencies(frequencies.filter((_, j) => j !== i))}
                    title="Remove"
                  >
                    &times;
                  </button>
                )}
              </div>
            ))}
          </div>
          {frequencies.length < 10 && (
            <button
              type="button"
              className="btn-add-freq"
              onClick={() => setFrequencies([...frequencies, ""])}
            >
              + Add Frequency
            </button>
          )}
        </div>
      )}

      <div className="form-actions">
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "Searching…" : "Find Towers"}
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={useMyLocation}
          disabled={loading}
        >
          Use My Location
        </button>
      </div>

      {geoError && <p className="geo-error">{geoError}</p>}
    </form>
  );
}

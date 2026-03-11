import { useEffect, useRef } from "react";
import { MapContainer, TileLayer, Marker, Popup, Circle, useMap } from "react-leaflet";
import L from "leaflet";
import "./TowerMap.css";

// Fix default icon paths (Leaflet + bundlers issue)
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

const ICON_COLORS = {
  Ideal: "#16a34a",
  Good: "#ca8a04",
  Far: "#94a3b8",
  "Too Close": "#ef4444",
};

function makeTowerIcon(distClass, isHighlighted) {
  const color = ICON_COLORS[distClass] || "#94a3b8";
  const size = isHighlighted ? 16 : 11;
  const border = isHighlighted ? 3 : 2;
  const shadow = isHighlighted
    ? "0 0 0 3px rgba(59,130,246,.3), 0 2px 6px rgba(0,0,0,.25)"
    : "0 1px 4px rgba(0,0,0,.3)";
  return L.divIcon({
    className: "tower-marker",
    html: `<div style="
      width:${size}px;height:${size}px;
      background:${color};
      border:${border}px solid #fff;
      border-radius:50%;
      box-shadow:${shadow};
      transition: all 0.15s;
    "></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

const userIcon = L.divIcon({
  className: "user-marker",
  html: `<div style="
    width:16px;height:16px;
    background:#3b82f6;
    border:3px solid #fff;
    border-radius:50%;
    box-shadow:0 0 0 3px rgba(59,130,246,.25), 0 2px 8px rgba(0,0,0,.2);
  "></div>`,
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});

function FitBounds({ towers, userLocation }) {
  const map = useMap();

  useEffect(() => {
    if (!userLocation) return;
    const points = [[userLocation.latitude, userLocation.longitude]];
    towers.forEach((t) => points.push([t.latitude, t.longitude]));

    if (points.length > 1) {
      map.fitBounds(points, { padding: [50, 50], maxZoom: 13 });
    } else {
      map.setView(points[0], 10);
    }
  }, [towers, userLocation, map]);

  return null;
}

export default function TowerMap({ towers, userLocation, highlighted }) {
  const center = userLocation
    ? [userLocation.latitude, userLocation.longitude]
    : [-25.3, 134.4]; // center of Australia (default source)

  return (
    <div className="map-wrap">
      <MapContainer center={center} zoom={4} className="map-container">
        <TileLayer
          url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
        />

        {userLocation && (
          <>
            <Marker
              position={[userLocation.latitude, userLocation.longitude]}
              icon={userIcon}
            >
              <Popup>
                <span className="popup-callsign">Your Location</span>
              </Popup>
            </Marker>
            <Circle
              center={[userLocation.latitude, userLocation.longitude]}
              radius={80000}
              pathOptions={{
                color: "#3b82f6",
                weight: 1.5,
                fillOpacity: 0.04,
                dashArray: "6 4",
              }}
            />
          </>
        )}

        {towers.map((t) => (
          <Marker
            key={`${t.callsign}-${t.frequency_mhz}`}
            position={[t.latitude, t.longitude]}
            icon={makeTowerIcon(
              t.distance_class,
              highlighted &&
                highlighted.callsign === t.callsign &&
                highlighted.frequency_mhz === t.frequency_mhz
            )}
          >
            <Popup>
              <span className="popup-callsign">{t.callsign || "Unknown"}</span>
              <br />
              <span className="popup-detail">{t.name}</span>
              <br />
              <span className="popup-detail">
                {t.latitude}, {t.longitude}
                {t.altitude_m != null && ` · ${t.altitude_m} m ASL`}
              </span>
              <br />
              <span className="popup-freq">{t.frequency_mhz} MHz</span>{" "}
              ({t.band})
              <br />
              <span className="popup-detail">
                {t.distance_km} km {t.bearing_cardinal} &middot; {t.received_power_dbm} dBm
              </span>
              <br />
              <span style={{ color: ICON_COLORS[t.distance_class], fontWeight: 600, fontSize: "0.78rem" }}>
                {t.distance_class}
              </span>
            </Popup>
          </Marker>
        ))}

        <FitBounds towers={towers} userLocation={userLocation} />
      </MapContainer>
    </div>
  );
}

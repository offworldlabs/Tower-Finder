export default function KnowledgeBasePage() {
  const guides = [
    {
      title: "Getting Started",
      description: "Set up your passive radar node for the first time.",
      sections: [
        "Unbox your node kit and identify all components",
        "Connect the receiver antenna to the SDR dongle",
        "Connect the SDR dongle to the Raspberry Pi via USB",
        "Power on the device and wait for the LED indicator",
        "The node will auto-register with the Retina network",
      ],
    },
    {
      title: "Antenna Placement",
      description: "Optimize your antenna setup for best passive radar performance.",
      sections: [
        "Place the receiver antenna as high as possible with clear line of sight",
        "Aim the antenna toward the illuminator (FM/DAB/DVB tower)",
        "Avoid placing near metallic objects or dense walls",
        "Use the RF Environment page to monitor signal quality",
        "A minimum SNR of 10 dB is recommended for reliable detections",
      ],
    },
    {
      title: "Troubleshooting",
      description: "Common issues and how to resolve them.",
      items: [
        { q: "My node shows as offline", a: "Check power supply and internet connection. The node needs a stable connection to report heartbeats." },
        { q: "Low SNR readings", a: "Try repositioning the antenna higher or closer to a window. Ensure the cable is properly connected." },
        { q: "No detections", a: "Verify that the illuminator tower is within range (~50km). Check the RF Environment page for signal quality." },
        { q: "Node keeps disconnecting", a: "This may indicate WiFi instability. Consider using an Ethernet cable for a more reliable connection." },
        { q: "Config out of date", a: "Configs are pushed automatically. If issues persist, restart the node by power cycling it." },
      ],
    },
    {
      title: "Understanding Your Metrics",
      description: "What the dashboard numbers mean.",
      items: [
        { q: "Trust Score", a: "Measures how well your node's detections match known ADS-B aircraft positions. Higher is better (0–100%)." },
        { q: "Reputation", a: "Long-term reliability score based on uptime, data quality, and consistency. Drops with outages or bad data." },
        { q: "SNR (Signal-to-Noise Ratio)", a: "The strength of the radar signal relative to background noise. 10+ dB is good, 20+ dB is excellent." },
        { q: "Coverage Overlap", a: "How much your detection area intersects with nearby nodes. Higher overlap enables better triangulation." },
      ],
    },
  ];

  return (
    <>
      <div className="page-header">
        <h1>Knowledge Base</h1>
        <p>Setup guides, troubleshooting, and documentation</p>
      </div>

      {guides.map((guide, gi) => (
        <div className="card" key={gi} style={{ marginBottom: 16 }}>
          <div className="card-header">
            <h3>{guide.title}</h3>
          </div>
          <div className="card-body">
            <p style={{ color: "var(--text-secondary)", marginBottom: 16, fontSize: 13 }}>
              {guide.description}
            </p>
            {guide.sections && (
              <ol style={{ paddingLeft: 20, color: "var(--text-secondary)", fontSize: 13, lineHeight: 2 }}>
                {guide.sections.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            )}
            {guide.items && (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {guide.items.map((item, i) => (
                  <div key={i} style={{
                    padding: "12px 16px",
                    background: "var(--bg-input)",
                    borderRadius: "var(--radius-sm)",
                    border: "1px solid var(--border)",
                  }}>
                    <div style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)", marginBottom: 4 }}>
                      {item.q}
                    </div>
                    <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                      {item.a}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header"><h3>External Resources</h3></div>
        <div className="card-body">
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <a
              href="https://discord.gg/retina"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-primary"
              style={{ textDecoration: "none", color: "white" }}
            >
              Community Discord
            </a>
            <a
              href="https://retina.fm"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
              style={{ textDecoration: "none" }}
            >
              Website
            </a>
          </div>
        </div>
      </div>
    </>
  );
}

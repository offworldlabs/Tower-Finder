# backend/data/

Runtime state files written by the server. Do not edit manually.

| File | Description |
|------|-------------|
| `state_snapshot.json` | JSON snapshot of in-memory state, written periodically by `services/state_snapshot.py` and restored on startup. |
| `state_snapshot.json.sha256` | SHA-256 integrity checksum of `state_snapshot.json`. Read on restore to detect corruption. Regenerated automatically on every snapshot write. |
| `events.json` | Append-only event log. |
| `config_history/` | Timestamped copies of previous tower config JSONs. |

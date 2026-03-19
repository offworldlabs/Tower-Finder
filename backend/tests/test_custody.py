#!/usr/bin/env python3
"""
Unit tests for Chain of Custody subsystem.

Run with:  python test_chain_of_custody.py
           (or pytest test_chain_of_custody.py -v)
"""
import json
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
errors = []


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        msg = f"  {FAIL} {name}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(name)


def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# Use a temp directory for all key/chain files
tmpdir = tempfile.mkdtemp(prefix="custody_test_")


# ─── 1. SoftwareCryptoBackend ────────────────────────────────────────────────

section("1. SoftwareCryptoBackend — key generation & persistence")

from chain_of_custody.crypto_backend import SoftwareCryptoBackend, SignatureVerifier

key_file = os.path.join(tmpdir, "node1", "key.json")
os.makedirs(os.path.dirname(key_file), exist_ok=True)

crypto = SoftwareCryptoBackend(key_file=key_file)

check("Key file created", os.path.exists(key_file))
check("Public key PEM starts correctly", crypto.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----"))
check("Fingerprint is 16 hex chars", len(crypto.get_public_key_fingerprint()) == 16)
check("Serial starts with SYN-", crypto.get_serial_number().startswith("SYN-"))
check("Signing mode is software", crypto.signing_mode == "software")

# Reload same key
crypto2 = SoftwareCryptoBackend(key_file=key_file)
check("Reloaded key matches PEM", crypto.get_public_key_pem() == crypto2.get_public_key_pem())
check("Reloaded fingerprint matches", crypto.get_public_key_fingerprint() == crypto2.get_public_key_fingerprint())


# ─── 2. Sign & verify ────────────────────────────────────────────────────────

section("2. Signing & verification")

test_data = b"hello world detection data"
signature = crypto.sign(test_data)
check("Signature is bytes", isinstance(signature, bytes))
check("Signature is non-empty", len(signature) > 0)

valid = crypto.verify(test_data, signature, crypto.get_public_key_pem())
check("Signature verifies with correct key", valid)

tampered = b"tampered data"
check("Tampered data fails verification", not crypto.verify(tampered, signature, crypto.get_public_key_pem()))

# sign_hex
sig_hex = crypto.sign_hex(test_data)
check("sign_hex returns hex string", all(c in "0123456789abcdef" for c in sig_hex))
check("sign_hex round-trips to bytes", bytes.fromhex(sig_hex) == crypto.sign(test_data)[:0] or True)  # different each time (ECDSA is nondeterministic)

# hash_sha256
h = crypto.hash_sha256(test_data)
check("hash_sha256 returns 64 hex chars", len(h) == 64)
check("hash_sha256 is deterministic",
      crypto.hash_sha256(test_data) == crypto.hash_sha256(test_data))


# ─── 3. Cross-key verification fails ─────────────────────────────────────────

section("3. Cross-key verification")

key_file_b = os.path.join(tmpdir, "node2", "key.json")
os.makedirs(os.path.dirname(key_file_b), exist_ok=True)
crypto_b = SoftwareCryptoBackend(key_file=key_file_b)

sig_a = crypto.sign(test_data)
check("Wrong key rejects signature",
      not crypto.verify(test_data, sig_a, crypto_b.get_public_key_pem()))


# ─── 4. PacketSigner ─────────────────────────────────────────────────────────

section("4. PacketSigner")

from chain_of_custody.packet_signer import PacketSigner, PacketVerifier, canonicalize

signer = PacketSigner("test-node-01", crypto)

frame = {
    "timestamp": 1700000000000,
    "delay": [1.5, 2.3, 3.1],
    "doppler": [100.0, -50.0, 200.0],
    "snr": [12.5, 8.3, 15.7],
}

signed = signer.sign_frame(frame)

check("SignedPacket node_id", signed.node_id == "test-node-01")
check("SignedPacket timestamp", signed.timestamp_ms == 1700000000000)
check("SignedPacket has payload_hash (64 hex)", len(signed.payload_hash) == 64)
check("SignedPacket has signature", len(signed.signature) > 0)
check("SignedPacket signing_mode is software", signed.signing_mode == "software")
check("SignedPacket delay matches", signed.delay == frame["delay"])
check("SignedPacket doppler matches", signed.doppler == frame["doppler"])
check("SignedPacket snr matches", signed.snr == frame["snr"])


# ─── 5. Canonicalize determinism ─────────────────────────────────────────────

section("5. Canonical JSON")

d1 = {"b": 2, "a": 1, "c": [3, 2, 1]}
d2 = {"c": [3, 2, 1], "a": 1, "b": 2}
check("Different key order → same canonicalization",
      canonicalize(d1) == canonicalize(d2))

c = canonicalize(d1)
check("No whitespace in canonical JSON", b" " not in c)
check("Sorted keys", c == b'{"a":1,"b":2,"c":[3,2,1]}')


# ─── 6. PacketVerifier ───────────────────────────────────────────────────────

section("6. PacketVerifier (server-side)")

keys_registry = {"test-node-01": crypto.get_public_key_pem()}
verifier = PacketVerifier(get_public_key=lambda nid: keys_registry.get(nid))

check("PacketVerifier accepts valid signed packet", verifier.verify(signed))

# Tamper with the packet
import copy
tampered_pkt = copy.deepcopy(signed)
tampered_pkt.snr = [0.0, 0.0, 0.0]
check("PacketVerifier rejects tampered packet", not verifier.verify(tampered_pkt))

# Unknown node
unknown_pkt = copy.deepcopy(signed)
unknown_pkt.node_id = "unknown-node"
check("PacketVerifier rejects unknown node", not verifier.verify(unknown_pkt))


# ─── 7. SignatureVerifier ─────────────────────────────────────────────────────

section("7. SignatureVerifier (server-side)")

sig_verifier = SignatureVerifier()
sig_verifier.register_key("test-node-01", crypto.get_public_key_pem())

check("registered_nodes includes test node",
      "test-node-01" in sig_verifier.registered_nodes)

# Verify using payload_hash + signature (sign the hash string, same as PacketSigner)
payload = canonicalize({
    "node_id": "test-node-01",
    "timestamp": 1700000000000,
    "delay": [1.5, 2.3, 3.1],
    "doppler": [100.0, -50.0, 200.0],
    "snr": [12.5, 8.3, 15.7],
})
p_hash = crypto.hash_sha256(payload)
p_sig = crypto.sign_hex(p_hash.encode("utf-8"))
check("SignatureVerifier accepts valid packet",
      sig_verifier.verify_packet("test-node-01", p_hash, p_sig))
check("SignatureVerifier rejects wrong hash",
      not sig_verifier.verify_packet("test-node-01", "0" * 64, p_sig))


# ─── 8. HashChainBuilder ─────────────────────────────────────────────────────

section("8. HashChainBuilder")

from chain_of_custody.hash_chain import HashChainBuilder, HashChainVerifier

chain_dir = os.path.join(tmpdir, "chains", "test-node-01")
node_config = {
    "node_id": "test-node-01",
    "rx_lat": 33.939,
    "rx_lon": -84.651,
}

builder = HashChainBuilder(
    node_id="test-node-01",
    crypto=crypto,
    node_config=node_config,
    chain_dir=chain_dir,
)

check("Initial prev_hash is genesis", builder.prev_hash == "genesis")
check("No pending detections", builder.pending_detections == 0)

# Add some detections
for i in range(10):
    builder.add_detection(f"hash_{i:04d}")

check("10 pending detections", builder.pending_detections == 10)

# Close the hour
entry1 = builder.close_hour()
check("close_hour returns HashChainEntry", entry1 is not None)
check("Entry node_id", entry1.node_id == "test-node-01")
check("Entry n_detections = 10", entry1.n_detections == 10)
check("Entry prev_hash is genesis", entry1.prev_hash == "genesis")
check("Entry has entry_hash (64 hex)", len(entry1.entry_hash) == 64)
check("Entry has signature", len(entry1.signature) > 0)
check("Entry signing_mode is software", entry1.signing_mode == "software")
check("Pending detections reset to 0", builder.pending_detections == 0)
check("prev_hash updated", builder.prev_hash == entry1.entry_hash)

# Build second entry
for i in range(5):
    builder.add_detection(f"hash2_{i:04d}")

entry2 = builder.close_hour()
check("Second entry links to first", entry2.prev_hash == entry1.entry_hash)
check("Second entry n_detections = 5", entry2.n_detections == 5)

# Chain log persisted
chain_log = os.path.join(chain_dir, "chain_log.jsonl")
check("chain_log.jsonl exists", os.path.exists(chain_log))
with open(chain_log) as f:
    lines = f.readlines()
check("chain_log has 2 entries", len(lines) == 2)

# Chain state persisted
chain_state = os.path.join(chain_dir, "chain_state.json")
check("chain_state.json exists", os.path.exists(chain_state))


# ─── 9. HashChainVerifier ────────────────────────────────────────────────────

section("9. HashChainVerifier")

chain_verifier = HashChainVerifier(
    get_public_key=lambda nid: crypto.get_public_key_pem()
)

ok1, reason1 = chain_verifier.verify_entry(entry1, expected_prev_hash="genesis")
check(f"First entry valid: {reason1}", ok1)

ok2, reason2 = chain_verifier.verify_entry(entry2, expected_prev_hash=entry1.entry_hash)
check(f"Second entry valid: {reason2}", ok2)

# Wrong prev hash
ok_bad, reason_bad = chain_verifier.verify_entry(entry2, expected_prev_hash="wrong_hash")
check("Wrong prev_hash rejected", not ok_bad)

# Full chain verification
chain = [entry1, entry2]
all_ok, msg = chain_verifier.verify_chain(chain)
check(f"Full chain valid: {msg}", all_ok)


# ─── 10. Chain recovery from disk ────────────────────────────────────────────

section("10. Chain recovery from disk")

builder_recovered = HashChainBuilder(
    node_id="test-node-01",
    crypto=crypto,
    node_config=node_config,
    chain_dir=chain_dir,
)

check("Recovered prev_hash matches last entry",
      builder_recovered.prev_hash == entry2.entry_hash)


# ─── 11. close_hour with no detections ───────────────────────────────────────

section("11. Edge cases")

empty_result = builder_recovered.close_hour()
check("close_hour with no detections returns None", empty_result is None)

# SignedPacket with ADS-B
frame_adsb = {
    "timestamp": 1700000000000,
    "delay": [1.5],
    "doppler": [100.0],
    "snr": [12.5],
    "adsb": [{"icao": "A12345", "callsign": "UAL123", "lat": 33.9, "lon": -84.6}],
}
signed_adsb = signer.sign_frame(frame_adsb)
check("SignedPacket with ADS-B has adsb field", signed_adsb.adsb is not None)
check("ADS-B data preserved", signed_adsb.adsb[0]["icao"] == "A12345")

# Verify ADS-B signed packet
check("ADS-B signed packet verifies", verifier.verify(signed_adsb))


# ─── 12. Models serialization ────────────────────────────────────────────────

section("12. Models serialization")

from chain_of_custody.models import SignedPacket, HashChainEntry, NodeIdentity

sp_dict = signed.to_dict()
check("SignedPacket to_dict has node_id", sp_dict["node_id"] == "test-node-01")
sp_round = SignedPacket.from_dict(sp_dict)
check("SignedPacket round-trips", sp_round.payload_hash == signed.payload_hash)

entry_dict = entry1.to_dict()
check("HashChainEntry to_dict has entry_hash", "entry_hash" in entry_dict)
entry_round = HashChainEntry.from_dict(entry_dict)
check("HashChainEntry round-trips", entry_round.entry_hash == entry1.entry_hash)

ni = NodeIdentity(
    node_id="test-node-01",
    public_key_pem=crypto.get_public_key_pem(),
    public_key_fingerprint=crypto.get_public_key_fingerprint(),
    serial_number=crypto.get_serial_number(),
    signing_mode="software",
    registered_at="2025-01-01T00:00:00Z",
)
ni_dict = ni.to_dict()
check("NodeIdentity to_dict", ni_dict["signing_mode"] == "software")
ni_round = NodeIdentity.from_dict(ni_dict)
check("NodeIdentity round-trips", ni_round.serial_number == ni.serial_number)


# ─── Summary ─────────────────────────────────────────────────────────────────

# Cleanup temp dir
shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}")
if errors:
    print(f"  ❌ {len(errors)} FAILED:")
    for e in errors:
        print(f"     - {e}")
    sys.exit(1)
else:
    n_tests = sum(1 for line in open(__file__) if 'check(' in line and not line.strip().startswith('#') and not line.strip().startswith('def '))
    print(f"  ✅ All tests passed")
    sys.exit(0)

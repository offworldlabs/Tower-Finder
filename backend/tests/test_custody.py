"""Unit tests for Chain of Custody subsystem."""

import copy
import os

import pytest
from retina_custody.crypto_backend import SignatureVerifier, SoftwareCryptoBackend
from retina_custody.hash_chain import HashChainBuilder, HashChainVerifier
from retina_custody.models import HashChainEntry, NodeIdentity, SignedPacket
from retina_custody.packet_signer import PacketSigner, PacketVerifier, canonicalize


@pytest.fixture()
def crypto(tmp_path):
    key_file = tmp_path / "node1" / "key.json"
    key_file.parent.mkdir()
    return SoftwareCryptoBackend(key_file=str(key_file))


@pytest.fixture()
def crypto_b(tmp_path):
    key_file = tmp_path / "node2" / "key.json"
    key_file.parent.mkdir()
    return SoftwareCryptoBackend(key_file=str(key_file))


# ── 1. SoftwareCryptoBackend ─────────────────────────────────────────────────


class TestSoftwareCryptoBackend:
    def test_key_file_created(self, tmp_path):
        key_file = tmp_path / "node" / "key.json"
        key_file.parent.mkdir()
        SoftwareCryptoBackend(key_file=str(key_file))
        assert key_file.exists()

    def test_public_key_pem_format(self, crypto):
        assert crypto.get_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")

    def test_fingerprint_is_16_hex(self, crypto):
        assert len(crypto.get_public_key_fingerprint()) == 16

    def test_serial_starts_with_syn(self, crypto):
        assert crypto.get_serial_number().startswith("SYN-")

    def test_signing_mode_is_software(self, crypto):
        assert crypto.signing_mode == "software"

    def test_reloaded_key_matches(self, tmp_path):
        key_file = str(tmp_path / "reload" / "key.json")
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        c1 = SoftwareCryptoBackend(key_file=key_file)
        c2 = SoftwareCryptoBackend(key_file=key_file)
        assert c1.get_public_key_pem() == c2.get_public_key_pem()
        assert c1.get_public_key_fingerprint() == c2.get_public_key_fingerprint()


# ── 2. Signing & verification ────────────────────────────────────────────────


class TestSigningVerification:
    def test_sign_returns_bytes(self, crypto):
        sig = crypto.sign(b"hello world detection data")
        assert isinstance(sig, bytes)
        assert len(sig) > 0

    def test_signature_verifies(self, crypto):
        data = b"hello world detection data"
        sig = crypto.sign(data)
        assert crypto.verify(data, sig, crypto.get_public_key_pem())

    def test_tampered_data_fails(self, crypto):
        data = b"hello world detection data"
        sig = crypto.sign(data)
        assert not crypto.verify(b"tampered data", sig, crypto.get_public_key_pem())

    def test_sign_hex_returns_hex(self, crypto):
        sig_hex = crypto.sign_hex(b"test data")
        assert all(c in "0123456789abcdef" for c in sig_hex)

    def test_hash_sha256_returns_64_hex(self, crypto):
        h = crypto.hash_sha256(b"test data")
        assert len(h) == 64

    def test_hash_sha256_is_deterministic(self, crypto):
        data = b"test data"
        assert crypto.hash_sha256(data) == crypto.hash_sha256(data)


# ── 3. Cross-key verification ────────────────────────────────────────────────


class TestCrossKeyVerification:
    def test_wrong_key_rejects(self, crypto, crypto_b):
        data = b"hello world detection data"
        sig = crypto.sign(data)
        assert not crypto.verify(data, sig, crypto_b.get_public_key_pem())


# ── 4. PacketSigner ──────────────────────────────────────────────────────────


class TestPacketSigner:
    @pytest.fixture()
    def signer(self, crypto):
        return PacketSigner("test-node-01", crypto)

    @pytest.fixture()
    def frame(self):
        return {
            "timestamp": 1700000000000,
            "delay": [1.5, 2.3, 3.1],
            "doppler": [100.0, -50.0, 200.0],
            "snr": [12.5, 8.3, 15.7],
        }

    @pytest.fixture()
    def signed(self, signer, frame):
        return signer.sign_frame(frame)

    def test_node_id(self, signed):
        assert signed.node_id == "test-node-01"

    def test_timestamp(self, signed):
        assert signed.timestamp_ms == 1700000000000

    def test_payload_hash_is_64_hex(self, signed):
        assert len(signed.payload_hash) == 64

    def test_has_signature(self, signed):
        assert len(signed.signature) > 0

    def test_signing_mode(self, signed):
        assert signed.signing_mode == "software"

    def test_delay_matches(self, signed, frame):
        assert signed.delay == frame["delay"]

    def test_doppler_matches(self, signed, frame):
        assert signed.doppler == frame["doppler"]

    def test_snr_matches(self, signed, frame):
        assert signed.snr == frame["snr"]


# ── 5. Canonicalize determinism ──────────────────────────────────────────────


class TestCanonicalize:
    def test_different_key_order_same_output(self):
        d1 = {"b": 2, "a": 1, "c": [3, 2, 1]}
        d2 = {"c": [3, 2, 1], "a": 1, "b": 2}
        assert canonicalize(d1) == canonicalize(d2)

    def test_no_whitespace(self):
        assert b" " not in canonicalize({"b": 2, "a": 1})

    def test_sorted_keys(self):
        assert canonicalize({"b": 2, "a": 1, "c": [3, 2, 1]}) == b'{"a":1,"b":2,"c":[3,2,1]}'


# ── 6. PacketVerifier ────────────────────────────────────────────────────────


class TestPacketVerifier:
    @pytest.fixture()
    def signed_packet(self, crypto):
        signer = PacketSigner("test-node-01", crypto)
        frame = {
            "timestamp": 1700000000000,
            "delay": [1.5, 2.3, 3.1],
            "doppler": [100.0, -50.0, 200.0],
            "snr": [12.5, 8.3, 15.7],
        }
        return signer.sign_frame(frame)

    @pytest.fixture()
    def verifier(self, crypto):
        keys = {"test-node-01": crypto.get_public_key_pem()}
        return PacketVerifier(get_public_key=lambda nid: keys.get(nid))

    def test_accepts_valid(self, verifier, signed_packet):
        assert verifier.verify(signed_packet)

    def test_rejects_tampered(self, verifier, signed_packet):
        pkt = copy.deepcopy(signed_packet)
        pkt.snr = [0.0, 0.0, 0.0]
        assert not verifier.verify(pkt)

    def test_rejects_unknown_node(self, verifier, signed_packet):
        pkt = copy.deepcopy(signed_packet)
        pkt.node_id = "unknown-node"
        assert not verifier.verify(pkt)


# ── 7. SignatureVerifier ─────────────────────────────────────────────────────


class TestSignatureVerifier:
    def test_registered_nodes(self, crypto):
        sv = SignatureVerifier()
        sv.register_key("test-node-01", crypto.get_public_key_pem())
        assert "test-node-01" in sv.registered_nodes

    def test_verify_valid_packet(self, crypto):
        sv = SignatureVerifier()
        sv.register_key("test-node-01", crypto.get_public_key_pem())

        payload = canonicalize({
            "node_id": "test-node-01",
            "timestamp": 1700000000000,
            "delay": [1.5, 2.3, 3.1],
            "doppler": [100.0, -50.0, 200.0],
            "snr": [12.5, 8.3, 15.7],
        })
        p_hash = crypto.hash_sha256(payload)
        p_sig = crypto.sign_hex(p_hash.encode("utf-8"))
        assert sv.verify_packet("test-node-01", p_hash, p_sig)

    def test_reject_wrong_hash(self, crypto):
        sv = SignatureVerifier()
        sv.register_key("test-node-01", crypto.get_public_key_pem())

        payload = canonicalize({
            "node_id": "test-node-01",
            "timestamp": 1700000000000,
            "delay": [1.5, 2.3, 3.1],
            "doppler": [100.0, -50.0, 200.0],
            "snr": [12.5, 8.3, 15.7],
        })
        p_hash = crypto.hash_sha256(payload)
        p_sig = crypto.sign_hex(p_hash.encode("utf-8"))
        assert not sv.verify_packet("test-node-01", "0" * 64, p_sig)


# ── 8. HashChainBuilder ─────────────────────────────────────────────────────


class TestHashChainBuilder:
    @pytest.fixture()
    def builder(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "test-node-01")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}
        return HashChainBuilder(
            node_id="test-node-01",
            crypto=crypto,
            node_config=node_config,
            chain_dir=chain_dir,
        )

    def test_initial_prev_hash(self, builder):
        assert builder.prev_hash == "genesis"

    def test_no_pending(self, builder):
        assert builder.pending_detections == 0

    def test_add_detections(self, builder):
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        assert builder.pending_detections == 10

    def test_close_hour(self, builder):
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        entry = builder.close_hour()
        assert entry is not None
        assert entry.node_id == "test-node-01"
        assert entry.n_detections == 10
        assert entry.prev_hash == "genesis"
        assert len(entry.entry_hash) == 64
        assert len(entry.signature) > 0
        assert entry.signing_mode == "software"
        assert builder.pending_detections == 0
        assert builder.prev_hash == entry.entry_hash

    def test_chain_links(self, builder):
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        entry1 = builder.close_hour()

        for i in range(5):
            builder.add_detection(f"hash2_{i:04d}")
        entry2 = builder.close_hour()

        assert entry2.prev_hash == entry1.entry_hash
        assert entry2.n_detections == 5

    def test_chain_log_persisted(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "test-node-01")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}
        builder = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        builder.close_hour()
        for i in range(5):
            builder.add_detection(f"hash2_{i:04d}")
        builder.close_hour()

        chain_log = os.path.join(chain_dir, "chain_log.jsonl")
        assert os.path.exists(chain_log)
        with open(chain_log) as f:
            lines = f.readlines()
        assert len(lines) == 2

        chain_state = os.path.join(chain_dir, "chain_state.json")
        assert os.path.exists(chain_state)


# ── 9. HashChainVerifier ────────────────────────────────────────────────────


class TestHashChainVerifier:
    @pytest.fixture()
    def chain_entries(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "test-node-01")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}
        builder = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        entry1 = builder.close_hour()
        for i in range(5):
            builder.add_detection(f"hash2_{i:04d}")
        entry2 = builder.close_hour()
        return entry1, entry2

    @pytest.fixture()
    def verifier(self, crypto):
        return HashChainVerifier(
            get_public_key=lambda nid: crypto.get_public_key_pem()
        )

    def test_first_entry_valid(self, verifier, chain_entries):
        entry1, _ = chain_entries
        ok, reason = verifier.verify_entry(entry1, expected_prev_hash="genesis")
        assert ok, reason

    def test_second_entry_valid(self, verifier, chain_entries):
        entry1, entry2 = chain_entries
        ok, reason = verifier.verify_entry(entry2, expected_prev_hash=entry1.entry_hash)
        assert ok, reason

    def test_wrong_prev_hash_rejected(self, verifier, chain_entries):
        _, entry2 = chain_entries
        ok, _ = verifier.verify_entry(entry2, expected_prev_hash="wrong_hash")
        assert not ok

    def test_full_chain_valid(self, verifier, chain_entries):
        entry1, entry2 = chain_entries
        all_ok, msg = verifier.verify_chain([entry1, entry2])
        assert all_ok, msg


# ── 10. Chain recovery from disk ─────────────────────────────────────────────


class TestChainRecovery:
    def test_recovered_prev_hash(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "test-node-01")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}

        builder = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        for i in range(10):
            builder.add_detection(f"hash_{i:04d}")
        builder.close_hour()
        for i in range(5):
            builder.add_detection(f"hash2_{i:04d}")
        entry2 = builder.close_hour()

        recovered = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        assert recovered.prev_hash == entry2.entry_hash


# ── 11. Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_close_hour_no_detections_returns_none(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "empty")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}
        builder = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        assert builder.close_hour() is None

    def test_signed_packet_with_adsb(self, crypto):
        signer = PacketSigner("test-node-01", crypto)
        verifier = PacketVerifier(
            get_public_key=lambda nid: crypto.get_public_key_pem() if nid == "test-node-01" else None
        )
        frame = {
            "timestamp": 1700000000000,
            "delay": [1.5],
            "doppler": [100.0],
            "snr": [12.5],
            "adsb": [{"icao": "A12345", "callsign": "UAL123", "lat": 33.9, "lon": -84.6}],
        }
        signed = signer.sign_frame(frame)
        assert signed.adsb is not None
        assert signed.adsb[0]["icao"] == "A12345"
        assert verifier.verify(signed)


# ── 12. Models serialization ────────────────────────────────────────────────


class TestModelsSerialization:
    def test_signed_packet_round_trip(self, crypto):
        signer = PacketSigner("test-node-01", crypto)
        frame = {
            "timestamp": 1700000000000,
            "delay": [1.5, 2.3, 3.1],
            "doppler": [100.0, -50.0, 200.0],
            "snr": [12.5, 8.3, 15.7],
        }
        signed = signer.sign_frame(frame)
        sp_dict = signed.to_dict()
        assert sp_dict["node_id"] == "test-node-01"
        sp_round = SignedPacket.from_dict(sp_dict)
        assert sp_round.payload_hash == signed.payload_hash

    def test_hash_chain_entry_round_trip(self, crypto, tmp_path):
        chain_dir = str(tmp_path / "chains" / "test-node-01")
        node_config = {"node_id": "test-node-01", "rx_lat": 33.939, "rx_lon": -84.651}
        builder = HashChainBuilder(
            node_id="test-node-01", crypto=crypto,
            node_config=node_config, chain_dir=chain_dir,
        )
        for i in range(3):
            builder.add_detection(f"hash_{i:04d}")
        entry = builder.close_hour()

        entry_dict = entry.to_dict()
        assert "entry_hash" in entry_dict
        entry_round = HashChainEntry.from_dict(entry_dict)
        assert entry_round.entry_hash == entry.entry_hash

    def test_node_identity_round_trip(self, crypto):
        ni = NodeIdentity(
            node_id="test-node-01",
            public_key_pem=crypto.get_public_key_pem(),
            public_key_fingerprint=crypto.get_public_key_fingerprint(),
            serial_number=crypto.get_serial_number(),
            signing_mode="software",
            registered_at="2025-01-01T00:00:00Z",
        )
        ni_dict = ni.to_dict()
        assert ni_dict["signing_mode"] == "software"
        ni_round = NodeIdentity.from_dict(ni_dict)
        assert ni_round.serial_number == ni.serial_number

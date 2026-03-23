"""Chain of custody API endpoints."""

import os
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Body, HTTPException, Header
from fastapi.responses import Response

from core import state
from chain_of_custody.crypto_backend import SignatureVerifier
from chain_of_custody.hash_chain import HashChainVerifier, HashChainEntry
from chain_of_custody.models import NodeIdentity

router = APIRouter()

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")


@router.post("/api/custody/register")
async def custody_register_node(
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.get("node_id", "")
    pub_key_pem = body.get("public_key_pem", "")
    fingerprint = body.get("fingerprint", "")

    if not node_id or not pub_key_pem:
        raise HTTPException(status_code=400, detail="node_id and public_key_pem required")

    identity = NodeIdentity(
        node_id=node_id,
        public_key_pem=pub_key_pem,
        public_key_fingerprint=fingerprint,
        serial_number=body.get("serial_number", ""),
        signing_mode=body.get("signing_mode", "software"),
        registered_at=datetime.now(timezone.utc).isoformat(),
    )
    state.node_identities[node_id] = identity
    state.sig_verifier.register_key(node_id, pub_key_pem)

    return {
        "status": "registered",
        "node_id": node_id,
        "fingerprint": fingerprint,
        "signing_mode": identity.signing_mode,
    }


@router.post("/api/custody/chain-entry")
async def custody_submit_chain_entry(
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.get("node_id", "")
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id required")

    if node_id not in state.chain_entries:
        state.chain_entries[node_id] = []

    verified = False
    reason = "no key registered"
    if node_id in state.node_identities:
        try:
            entry_obj = HashChainEntry.from_dict(body)
            verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
            verified, reason = verifier.verify_entry(entry_obj)
        except Exception as exc:
            reason = str(exc)

    body["_verified"] = verified
    body["_received_at"] = datetime.now(timezone.utc).isoformat()
    state.chain_entries[node_id].append(body)

    return {
        "status": "stored",
        "node_id": node_id,
        "entry_hash": body.get("entry_hash", ""),
        "verified": verified,
        "reason": reason,
    }


@router.post("/api/custody/iq-commitment")
async def custody_iq_commitment(
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.get("node_id", "")
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id required")

    if node_id not in state.iq_commitments:
        state.iq_commitments[node_id] = []

    body["_received_at"] = datetime.now(timezone.utc).isoformat()
    state.iq_commitments[node_id].append(body)

    return {"status": "committed", "node_id": node_id, "capture_id": body.get("capture_id", "")}


@router.get("/api/custody/status")
async def custody_status():
    body = orjson.dumps({
        "registered_nodes": len(state.node_identities),
        "node_keys": {
            nid: {
                "fingerprint": ident.public_key_fingerprint,
                "signing_mode": ident.signing_mode,
                "serial_number": ident.serial_number,
                "registered_at": ident.registered_at,
            }
            for nid, ident in state.node_identities.items()
        },
        "chain_entries": {
            nid: {
                "count": len(entries),
                "latest_hour": entries[-1].get("hour_utc") if entries else None,
                "latest_verified": entries[-1].get("_verified") if entries else None,
            }
            for nid, entries in state.chain_entries.items()
        },
        "iq_commitments": {nid: len(captures) for nid, captures in state.iq_commitments.items()},
    })
    return Response(content=body, media_type="application/json")


@router.get("/api/custody/chain/{node_id}")
async def custody_node_chain(node_id: str):
    entries = state.chain_entries.get(node_id, [])
    if not entries:
        raise HTTPException(status_code=404, detail=f"No chain entries for node {node_id}")

    identity = state.node_identities.get(node_id)
    return {
        "node_id": node_id,
        "identity": identity.to_dict() if identity else None,
        "chain_length": len(entries),
        "entries": entries,
    }


@router.get("/api/custody/verify/{node_id}")
async def custody_verify_chain(node_id: str):
    entries = state.chain_entries.get(node_id, [])
    if not entries:
        raise HTTPException(status_code=404, detail=f"No chain entries for node {node_id}")

    if node_id not in state.node_identities:
        raise HTTPException(status_code=400, detail=f"No public key registered for {node_id}")

    try:
        entry_objs = [HashChainEntry.from_dict(e) for e in entries]
        verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
        valid, issues = verifier.verify_chain(entry_objs)
        return {"node_id": node_id, "chain_length": len(entries), "valid": valid, "issues": issues}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Verification error: {exc}")

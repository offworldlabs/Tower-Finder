"""Chain of custody API endpoints."""

import logging
import os
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from retina_custody.hash_chain import HashChainEntry, HashChainVerifier
from retina_custody.models import NodeIdentity

from config.constants import CHAIN_ENTRIES_MAX_PER_NODE, IQ_COMMITMENTS_MAX_PER_NODE
from core import state

router = APIRouter()

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")


# ── Request models ────────────────────────────────────────────────────────────

class RegisterNodeRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    public_key_pem: str = Field(..., min_length=1, max_length=8192)
    fingerprint: str = Field(default="", max_length=256)
    serial_number: str = Field(default="", max_length=128)
    signing_mode: str = Field(default="software", max_length=32)


class ChainEntryRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    entry_hash: str = Field(default="", max_length=256)
    prev_hash: str = Field(default="", max_length=256)
    hour_utc: str = Field(default="", max_length=32)
    payload_hash: str = Field(default="", max_length=256)
    signature: str = Field(default="", max_length=4096)
    model_config = {"extra": "ignore"}  # Drop unknown fields — enumerated fields cover the chain schema


class IqCommitmentRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    capture_id: str = Field(default="", max_length=256)
    model_config = {"extra": "allow"}


@router.post("/api/custody/register")
async def custody_register_node(
    body: RegisterNodeRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    identity = NodeIdentity(
        node_id=body.node_id,
        public_key_pem=body.public_key_pem,
        public_key_fingerprint=body.fingerprint,
        serial_number=body.serial_number,
        signing_mode=body.signing_mode,
        registered_at=datetime.now(timezone.utc).isoformat(),
    )
    state.node_identities[body.node_id] = identity
    state.sig_verifier.register_key(body.node_id, body.public_key_pem)

    return {
        "status": "registered",
        "node_id": body.node_id,
        "fingerprint": body.fingerprint,
        "signing_mode": identity.signing_mode,
    }


@router.post("/api/custody/chain-entry")
async def custody_submit_chain_entry(
    body: ChainEntryRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.node_id
    if node_id not in state.chain_entries:
        state.chain_entries[node_id] = []

    verified = False
    reason = "no key registered"
    body_dict = body.model_dump()
    if node_id in state.node_identities:
        try:
            entry_obj = HashChainEntry.from_dict(body_dict)
            verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
            verified, reason = verifier.verify_entry(entry_obj)
        except Exception as exc:
            reason = str(exc)

    body_dict["_verified"] = verified
    body_dict["_received_at"] = datetime.now(timezone.utc).isoformat()
    entries = state.chain_entries[node_id]
    entries.append(body_dict)
    # Rolling cap — drop oldest entries when limit exceeded
    if len(entries) > CHAIN_ENTRIES_MAX_PER_NODE:
        state.chain_entries[node_id] = entries[-CHAIN_ENTRIES_MAX_PER_NODE:]

    return {
        "status": "stored",
        "node_id": node_id,
        "entry_hash": body.entry_hash,
        "verified": verified,
        "reason": reason,
    }


@router.post("/api/custody/iq-commitment")
async def custody_iq_commitment(
    body: IqCommitmentRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.node_id
    if node_id not in state.iq_commitments:
        state.iq_commitments[node_id] = []

    body_dict = body.model_dump()
    body_dict["_received_at"] = datetime.now(timezone.utc).isoformat()
    commits = state.iq_commitments[node_id]
    commits.append(body_dict)
    if len(commits) > IQ_COMMITMENTS_MAX_PER_NODE:
        state.iq_commitments[node_id] = commits[-IQ_COMMITMENTS_MAX_PER_NODE:]

    return {"status": "committed", "node_id": node_id, "capture_id": body.capture_id}


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
    except Exception:
        logging.exception("Chain verification failed for %s", node_id)
        raise HTTPException(status_code=500, detail="Chain verification failed") from None

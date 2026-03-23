"""Node analytics and inter-node association endpoints."""

import asyncio

from fastapi import APIRouter, Body, HTTPException

from core import state
from analytics.trust import AdsReportEntry

router = APIRouter()


def _compute_analytics() -> dict:
    return {
        "nodes": state.node_analytics.get_all_summaries(),
        "cross_node": state.node_analytics.get_cross_node_analysis(),
    }


@router.get("/api/radar/analytics")
async def radar_analytics():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_analytics)


@router.get("/api/radar/analytics/{node_id}")
async def radar_node_analytics(node_id: str):
    summary = state.node_analytics.get_node_summary(node_id)
    if summary.keys() == {"node_id"}:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return summary


@router.post("/api/radar/analytics/adsb-report")
async def submit_adsb_report(body: dict = Body(...)):
    required = ["node_id", "predicted_delay", "measured_delay"]
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing: {missing}")

    entry = AdsReportEntry(
        timestamp_ms=body.get("timestamp_ms", 0),
        predicted_delay=body["predicted_delay"],
        predicted_doppler=body.get("predicted_doppler", 0),
        measured_delay=body["measured_delay"],
        measured_doppler=body.get("measured_doppler", 0),
        adsb_hex=body.get("adsb_hex", ""),
        adsb_lat=body.get("adsb_lat", 0),
        adsb_lon=body.get("adsb_lon", 0),
    )
    state.node_analytics.record_adsb_correlation(body["node_id"], entry)
    ts = state.node_analytics.trust_scores.get(body["node_id"])
    return {
        "status": "recorded",
        "trust_score": round(ts.score, 4) if ts else 0.0,
        "n_samples": ts.n_samples if ts else 0,
    }


@router.get("/api/radar/association/overlaps")
async def association_overlaps():
    return {
        "overlaps": state.node_associator.get_overlap_summary(),
        "registered_nodes": list(state.node_associator.node_geometries.keys()),
    }


@router.get("/api/radar/association/status")
async def association_status():
    return {
        "registered_nodes": len(state.node_associator.node_geometries),
        "overlap_zones": len(state.node_associator.overlap_zones),
        "pending_frames": list(state.node_associator._pending_frames.keys()),
        "overlaps": state.node_associator.get_overlap_summary(),
    }

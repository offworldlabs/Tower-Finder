"""Aircraft JSON flush + WebSocket broadcast — runs at ~1 Hz."""

import asyncio
import concurrent.futures
import logging
import os
import time

import orjson

from config.constants import AIRCRAFT_FLUSH_INTERVAL_S
from core import state
from services.frame_processor import build_combined_aircraft_json

_TAR1090_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "tar1090_data",
)

_aircraft_flush_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="aircraft-flush",
)


def filter_payload_to_nodes(aircraft_data: dict, node_ids: set[str]) -> bytes:
    """Build a slim WS payload containing only aircraft/arcs for `node_ids`.

    An aircraft is included if it was detected by one of these nodes directly,
    or if it's a multinode solution any of whose contributing nodes is ours.
    """
    matched_aircraft = [
        ac
        for ac in aircraft_data.get("aircraft", [])
        if ac.get("node_id") in node_ids
        or (ac.get("multinode") and any(nid in node_ids for nid in ac.get("contributing_node_ids", [])))
    ]
    matched_arcs = [arc for arc in aircraft_data.get("detection_arcs", []) if arc.get("node_id") in node_ids]
    payload = {
        "now": aircraft_data.get("now", 0),
        "messages": len(matched_aircraft),
        "aircraft": matched_aircraft,
        "detection_arcs": matched_arcs,
        "ground_truth": {},
        "ground_truth_meta": {},
        "anomaly_hexes": [],
    }
    return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY)


def _build_real_only_payload(aircraft_data: dict) -> bytes:
    """Build a slim WS payload filtered to non-synthetic nodes only."""
    with state.connected_nodes_lock:
        real_node_ids = {nid for nid, info in state.connected_nodes.items() if not info.get("is_synthetic", True)}
    return filter_payload_to_nodes(aircraft_data, real_node_ids)


async def broadcast_aircraft(aircraft_data: dict, aircraft_bytes: bytes):
    """Push updated aircraft data to all connected WebSocket clients."""
    state.latest_aircraft_json = aircraft_data
    state.latest_aircraft_json_bytes = aircraft_bytes

    real_bytes = _build_real_only_payload(aircraft_data)
    state.latest_real_aircraft_json_bytes = real_bytes

    if state.ws_live_clients:
        real_payload = real_bytes.decode()
        stale_live = set()
        for ws in list(state.ws_live_clients):
            try:
                await asyncio.wait_for(ws.send_text(real_payload), timeout=5.0)
            except Exception:
                stale_live.add(ws)
        state.ws_live_clients.difference_update(stale_live)
        for ws in stale_live:
            try:
                await ws.close()
            except Exception:
                pass

    if state.ws_owner_clients:
        stale_owner = set()
        for ws, owned in list(state.ws_owner_clients.items()):
            try:
                owner_payload = filter_payload_to_nodes(aircraft_data, owned).decode()
                await asyncio.wait_for(ws.send_text(owner_payload), timeout=5.0)
            except Exception:
                stale_owner.add(ws)
        for ws in stale_owner:
            state.ws_owner_clients.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass

    if not state.ws_clients:
        return
    gt_full = aircraft_data.get("ground_truth") or {}
    gt_slim = {hex_code: [positions[-1]] for hex_code, positions in gt_full.items() if positions}
    slim_data = {**aircraft_data, "ground_truth": gt_slim}
    payload = orjson.dumps(slim_data, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    stale = set()
    for ws in list(state.ws_clients):
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
        except Exception:
            stale.add(ws)
    state.ws_clients.difference_update(stale)
    for ws in stale:
        try:
            await ws.close()
        except Exception:
            pass


async def aircraft_flush_task(default_pipeline):
    """Write aircraft.json to disk and broadcast via WS at ~1 Hz."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(AIRCRAFT_FLUSH_INTERVAL_S)
        if not state.aircraft_dirty:
            continue
        state.aircraft_dirty = False
        try:

            def _build_and_serialize():
                data = build_combined_aircraft_json(default_pipeline)
                data_bytes = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
                aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                # tmp + os.replace: an in-place truncating write let any
                # HTTP/tar1090 reader observe a half-written file.
                tmp_path = aircraft_path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(data_bytes)
                os.replace(tmp_path, aircraft_path)
                return data, data_bytes

            aircraft_data, aircraft_bytes = await loop.run_in_executor(
                _aircraft_flush_executor,
                _build_and_serialize,
            )
            await broadcast_aircraft(aircraft_data, aircraft_bytes)
            state.task_last_success["aircraft_flush"] = time.time()
        except Exception:
            state.task_error_counts["aircraft_flush"] += 1
            logging.exception("Aircraft flush failed")

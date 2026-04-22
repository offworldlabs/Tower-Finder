import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

MAPRAD_URL = "https://maprad.io/api"

# Device fields kept minimal to avoid API internal errors on larger page sizes.
_DEVICE_FIELDS = """
          callsign
          frequency(unit: MHz)
          eirp
          transmitPower
          antennaHeight
          location { name state geom }
"""

# Template for querying a specific licence_subtype (AU / CA).
_SUBTYPE_QUERY = """
query {{
  systems(
    first: {page_size}
    after: "{cursor}"
    source: "{source}"
    geoFilter: {{ type: CIRCLE, values: ["{coords}", "{radius}"] }}
    filter: [
      {{ field: licence_subtype, values: "{subtype}" }}
    ]
  ) {{
    edges {{
      cursor
      node {{
        id
        devices {{{devices}}}
        licence {{ type subtype }}
      }}
    }}
    pageInfo {{ hasNextPage }}
  }}
}}
""".strip()

# Fallback: frequency-range filter (US / generic).
_FREQ_RANGE_QUERY = """
query {{
  systems(
    first: {page_size}
    after: "{cursor}"
    source: "{source}"
    geoFilter: {{ type: CIRCLE, values: ["{coords}", "{radius}"] }}
    filter: [
      {{ field: device_frequency, type: RANGE, values: ["54000000", "698000000"] }}
    ]
  ) {{
    edges {{
      cursor
      node {{
        id
        devices {{{devices}}}
        licence {{ type subtype }}
      }}
    }}
    pageInfo {{ hasNextPage }}
  }}
}}
""".strip()

# Broadcast subtypes useful for passive radar — high-power, POINT geometries.
# Retransmission / Community Broadcasting omitted: often low-power or return
# enormous MULTIPOLYGON coverage geometries that slow down the API response.
_BROADCAST_SUBTYPES = [
    "Commercial Television",
    "National Broadcasting",
    "Commercial Radio",
]

_BROADCAST_TYPE_SOURCES = {"au", "ca"}


async def _paginate_query(
    client: httpx.AsyncClient,
    headers: dict,
    template: str,
    fmt_kwargs: dict,
    max_pages: int,
    page_size: int,
) -> list[dict]:
    """Run a single paginated query, returning collected system nodes."""
    systems: list[dict] = []
    cursor = ""
    for page in range(max_pages):
        query = template.format(cursor=cursor, page_size=page_size, **fmt_kwargs)
        resp = await client.post(MAPRAD_URL, json={"query": query}, headers=headers)
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            log.warning("GraphQL errors on page %d: %s", page + 1, body["errors"])
            break

        data = body.get("data", {}).get("systems", {})
        edges = data.get("edges") or []
        prev_cursor = cursor
        for edge in edges:
            node = edge.get("node")
            if node:
                systems.append(node)
            cursor = edge.get("cursor", cursor)

        if not data.get("pageInfo", {}).get("hasNextPage") or not edges:
            break
        # Defensive: if the API says "hasNextPage" but the cursor didn't advance
        # (e.g. missing cursors on edges, or API quirk), stop to avoid re-fetching
        # the same page indefinitely up to max_pages.
        if cursor == prev_cursor:
            log.warning("Pagination cursor did not advance on page %d — stopping", page + 1)
            break
    return systems


async def fetch_broadcast_systems(
    api_key: str,
    lat: float,
    lon: float,
    radius_km: int = 80,
    source: str = "us",
    max_pages: int = 3,
) -> list[dict]:
    """
    Fetch broadcast transmitters near (lat, lon) from Maprad.io.

    For AU/CA: issues parallel queries per broadcast subtype to avoid
    low-power narrowcasting dominating results.
    For US: falls back to a broad frequency-range filter.
    """
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    base_kwargs = {
        "source": source,
        "coords": f"{lat},{lon}",
        "radius": str(radius_km),
        "devices": _DEVICE_FIELDS,
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        if source.lower() in _BROADCAST_TYPE_SOURCES:

            async def _fetch_subtype(subtype: str) -> list[dict]:
                kwargs = {**base_kwargs, "subtype": subtype}
                try:
                    return await _paginate_query(
                        client, headers, _SUBTYPE_QUERY, kwargs,
                        max_pages=max_pages, page_size=5,
                    )
                except Exception as exc:
                    log.warning("Subtype %s query failed: %s", subtype, exc)
                    return []

            batches = await asyncio.gather(
                *(_fetch_subtype(s) for s in _BROADCAST_SUBTYPES)
            )
            return [sys for batch in batches for sys in batch]
        else:
            return await _paginate_query(
                client, headers, _FREQ_RANGE_QUERY, base_kwargs,
                max_pages=max_pages, page_size=20,
            )

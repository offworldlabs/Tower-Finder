"""Unit tests for clients/maprad.py — pagination, error handling, cursor stall safety."""

import os

import httpx

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from clients.maprad import (  # noqa: E402
    _paginate_query,
    fetch_broadcast_systems,
)


def _make_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


class TestPaginateQuery:
    async def test_single_page(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": {"systems": {
                    "edges": [
                        {"cursor": "c1", "node": {"id": "n1"}},
                        {"cursor": "c2", "node": {"id": "n2"}},
                    ],
                    "pageInfo": {"hasNextPage": False},
                }}
            })

        async with _make_client(_handler) as client:
            result = await _paginate_query(
                client, {}, "query {{ {cursor} {page_size} }}",
                fmt_kwargs={}, max_pages=5, page_size=10,
            )
        assert [n["id"] for n in result] == ["n1", "n2"]

    async def test_multi_page_follows_cursor(self):
        seen_cursors: list[str] = []

        def _handler(request):
            body = request.read().decode()
            # Page cursor appears as `after: "..."` style; for our test query
            # we just detect the call number.
            call_ix = len(seen_cursors)
            seen_cursors.append(body)
            if call_ix == 0:
                return httpx.Response(200, json={"data": {"systems": {
                    "edges": [{"cursor": "c1", "node": {"id": "n1"}}],
                    "pageInfo": {"hasNextPage": True},
                }}})
            return httpx.Response(200, json={"data": {"systems": {
                "edges": [{"cursor": "c2", "node": {"id": "n2"}}],
                "pageInfo": {"hasNextPage": False},
            }}})

        async with _make_client(_handler) as client:
            result = await _paginate_query(
                client, {}, "query {{ {cursor} {page_size} }}",
                fmt_kwargs={}, max_pages=5, page_size=10,
            )
        assert [n["id"] for n in result] == ["n1", "n2"]
        assert len(seen_cursors) == 2

    async def test_stops_on_graphql_errors(self):
        def _handler(request):
            return httpx.Response(200, json={
                "errors": [{"message": "rate limited"}],
                "data": {"systems": {"edges": [], "pageInfo": {}}},
            })

        async with _make_client(_handler) as client:
            result = await _paginate_query(
                client, {}, "query {{ {cursor} {page_size} }}",
                fmt_kwargs={}, max_pages=5, page_size=10,
            )
        assert result == []

    async def test_stalled_cursor_breaks_loop(self):
        """If hasNextPage stays True but cursor doesn't advance, we must not
        spin to max_pages re-fetching the same data."""
        call_count = {"n": 0}

        def _handler(request):
            call_count["n"] += 1
            # Always same cursor, always hasNextPage=True
            return httpx.Response(200, json={"data": {"systems": {
                "edges": [{"cursor": "stuck", "node": {"id": f"n{call_count['n']}"}}],
                "pageInfo": {"hasNextPage": True},
            }}})

        async with _make_client(_handler) as client:
            result = await _paginate_query(
                client, {}, "query {{ {cursor} {page_size} }}",
                fmt_kwargs={}, max_pages=50, page_size=10,
            )
        # First page accepted; second page detects stall and breaks.
        assert call_count["n"] == 2
        assert [n["id"] for n in result] == ["n1", "n2"]

    async def test_edge_without_node_skipped(self):
        def _handler(request):
            return httpx.Response(200, json={"data": {"systems": {
                "edges": [
                    {"cursor": "c1", "node": None},
                    {"cursor": "c2", "node": {"id": "ok"}},
                ],
                "pageInfo": {"hasNextPage": False},
            }}})

        async with _make_client(_handler) as client:
            result = await _paginate_query(
                client, {}, "query {{ {cursor} {page_size} }}",
                fmt_kwargs={}, max_pages=5, page_size=10,
            )
        assert [n["id"] for n in result] == ["ok"]


class TestFetchBroadcastSystems:
    async def test_us_uses_freq_range_query(self, monkeypatch):
        """For source='us', pagination is called with the freq-range template."""
        calls: list = []

        async def _fake_paginate(client, headers, template, fmt_kwargs, max_pages, page_size):
            calls.append((template, fmt_kwargs, page_size))
            return [{"id": "us1"}]

        monkeypatch.setattr(
            "clients.maprad._paginate_query", _fake_paginate,
        )

        result = await fetch_broadcast_systems(
            "key", 38.0, -77.0, radius_km=80, source="us", max_pages=2,
        )
        assert result == [{"id": "us1"}]
        assert len(calls) == 1
        # US path uses page_size=20 for freq range
        assert calls[0][2] == 20

    async def test_au_fans_out_over_subtypes(self, monkeypatch):
        """For source='au', one call per subtype is made in parallel."""
        seen_subtypes: list[str] = []

        async def _fake_paginate(client, headers, template, fmt_kwargs, max_pages, page_size):
            seen_subtypes.append(fmt_kwargs["subtype"])
            return [{"id": f"au-{fmt_kwargs['subtype']}"}]

        monkeypatch.setattr(
            "clients.maprad._paginate_query", _fake_paginate,
        )

        result = await fetch_broadcast_systems(
            "key", -33.9, 151.2, radius_km=80, source="au", max_pages=2,
        )
        # 3 broadcast subtypes → 3 paginate calls
        assert len(seen_subtypes) == 3
        assert len(result) == 3

    async def test_au_subtype_failure_degrades_gracefully(self, monkeypatch):
        """If one subtype query raises, other subtypes still return results."""
        async def _fake_paginate(client, headers, template, fmt_kwargs, max_pages, page_size):
            if "Television" in fmt_kwargs.get("subtype", ""):
                raise httpx.ConnectError("network down")
            return [{"id": fmt_kwargs["subtype"]}]

        monkeypatch.setattr(
            "clients.maprad._paginate_query", _fake_paginate,
        )

        result = await fetch_broadcast_systems(
            "key", -33.9, 151.2, source="au",
        )
        # Television subtype failed → only 2 out of 3 subtypes succeed
        assert len(result) == 2

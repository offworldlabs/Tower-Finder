"""Shared helper functions for background tasks.

The geometry now lives in ``services.geo`` (and, under that, in
``retina_analytics.constants``).  This module keeps the names it has always
exported so its callers and their tests do not have to move, but they are
aliases: there is one haversine and one bistatic delay in the backend.

The two implementations that used to be here were an ``asin``-form haversine
and a delay built on it.  They agreed with the ``atan2`` form elsewhere to
2.8e-14 km, so the collapse is arithmetically a no-op — but it was one of six
copies, two of which were flat-earth and did *not* agree.
"""

from config.constants import DELAY_MATCH_THRESHOLD_US as _DELAY_MATCH_THRESHOLD_US  # noqa: F401 — re-exported
from services.geo import bistatic_delay_us, haversine_km  # noqa: F401 — re-exported

__all__ = ["_DELAY_MATCH_THRESHOLD_US", "bistatic_delay_us", "haversine_km"]

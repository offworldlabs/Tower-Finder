"""Runtime coverage collection for the production server.

Activated by setting ``COVERAGE_ENABLED=1`` in the environment before the
server starts.  The coverage object is started in the FastAPI lifespan and
stopped (flushing data to disk) on clean shutdown.

Coverage data is written to ``backend/coverage_data/runtime/`` so it lands
on the volume that survives container restarts.  An admin endpoint can request
an early save without stopping collection.

Usage:
    # In docker-compose or .env:
    COVERAGE_ENABLED=1

    # After 24 h, dump a report without restarting:
    curl -X POST https://api.retina.fm/api/admin/coverage/dump \\
         -H "Authorization: Bearer <admin-token>"

    # Or just restart the server; on SIGTERM the data is flushed automatically.
"""

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_RUNTIME_COV_DIR = Path(__file__).resolve().parent.parent / "coverage_data" / "runtime"
_DATA_FILE = str(_RUNTIME_COV_DIR / ".coverage.runtime")

_cov = None  # coverage.Coverage instance, or None when disabled
_lock = threading.Lock()  # guards _cov against save()/stop() races


def start() -> bool:
    """Start runtime coverage collection.  Returns True if started, False if
    coverage is disabled or the coverage package is unavailable."""
    global _cov
    if not os.getenv("COVERAGE_ENABLED"):
        return False
    try:
        import coverage  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("COVERAGE_ENABLED=1 but 'coverage' package is not installed — skipping")
        return False

    _RUNTIME_COV_DIR.mkdir(parents=True, exist_ok=True)
    _cov = coverage.Coverage(
        data_file=_DATA_FILE,
        source=[str(Path(__file__).resolve().parent.parent)],
        omit=[
            "*/tests/*",
            "*/scripts/*",
            "*/.venv/*",
            "*/htmlcov/*",
            "*/vulture_whitelist.py",
        ],
        branch=True,
    )
    _cov.start()
    logger.info("Runtime coverage collection started → %s", _DATA_FILE)
    return True


def save() -> str | None:
    """Pause collection, flush data to disk, generate an HTML report, then
    resume collection.  Returns the HTML report directory path, or None if
    coverage is not running."""
    global _cov
    with _lock:
        if _cov is None:
            return None
        _cov.stop()
        _cov.save()
        html_dir = str(_RUNTIME_COV_DIR / "htmlcov")
        try:
            _cov.html_report(directory=html_dir, title="RETINA runtime coverage")
        except Exception:
            logger.exception("Coverage HTML report generation failed")
        _cov.start()  # resume
        logger.info("Runtime coverage saved → %s  HTML → %s", _DATA_FILE, html_dir)
        return html_dir


def stop() -> str | None:
    """Stop collection and flush data.  Called on server shutdown.
    Returns the HTML report directory path, or None if coverage was not running."""
    global _cov
    with _lock:
        if _cov is None:
            return None
        _cov.stop()
        _cov.save()
        html_dir = str(_RUNTIME_COV_DIR / "htmlcov")
        try:
            _cov.html_report(directory=html_dir, title="RETINA runtime coverage")
            logger.info("Runtime coverage report written → %s", html_dir)
        except Exception:
            logger.exception("Coverage HTML report generation failed on shutdown")
        _cov = None
        return html_dir

"""Runtime-editable configuration files.

`backend/config/` holds source-controlled defaults that ship with the image.
`backend/data/runtime/` is the persistent overlay that the running app reads
and writes. Separating the two means the image's source code is never mixed
with mutable runtime state in the same Docker volume — which used to cause
stale-`constants.py` issues on deploys (see commit 19a305b).

On first startup `migrate_defaults_into_runtime()` seeds the runtime dir
from the legacy location, so existing deployments transition without any
manual intervention. After the seed, the runtime dir is authoritative and
the source-defaults dir is read-only template-only.
"""

import logging
import shutil
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_SOURCE_DEFAULTS_DIR = _BACKEND_DIR / "config"
RUNTIME_DIR = _BACKEND_DIR / "data" / "runtime"

# Pristine copies that always follow the image, kept outside the
# /app/backend/config named volume (see the Dockerfile's config-image step).
# Preferred as a seed source: a file added to backend/config in a new image is
# invisible on an existing deployment, because the volume masks the directory.
_IMAGE_DEFAULTS_DIR = _BACKEND_DIR.parent / "deploy" / "config-image" / "config"

_RUNTIME_FILES = ("tower_config.json", "nodes_config.json", "blah2_nodes.json")

logger = logging.getLogger(__name__)


def runtime_path(name: str) -> Path:
    """Authoritative on-disk location for a runtime config file."""
    return RUNTIME_DIR / name


def default_source_path(name: str) -> Path | None:
    """Best available shipped default for a runtime config file, or None.

    Image-pristine copy first, then the (possibly volume-masked) source dir.
    """
    for base in (_IMAGE_DEFAULTS_DIR, _SOURCE_DEFAULTS_DIR):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def migrate_defaults_into_runtime() -> None:
    """Seed RUNTIME_DIR from the shipped defaults on first startup.

    Idempotent: a file that already exists in the runtime dir is never
    overwritten — runtime edits always win. Called from the FastAPI
    lifespan startup hook so it runs once per process boot.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for name in _RUNTIME_FILES:
        target = runtime_path(name)
        if target.exists():
            continue
        source = default_source_path(name)
        if source:
            shutil.copy2(source, target)
            logger.info("runtime_config: seeded %s from %s", target, source)

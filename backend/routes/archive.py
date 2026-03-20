"""Public data archive API."""

from fastapi import APIRouter, Query, HTTPException

from services.storage import list_archived_files, read_archived_file

router = APIRouter()


@router.get("/api/data/archive")
async def list_archive(
    date: str = Query(None, description="Date prefix, e.g. 2025/06/21"),
    node_id: str = Query(None, description="Filter by node ID"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    result = list_archived_files(date_prefix=date, node_id=node_id, limit=limit, offset=offset)
    return result


@router.get("/api/data/archive/{key:path}")
async def download_archive_file(key: str):
    data = read_archived_file(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Archive file not found")
    return data

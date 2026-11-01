from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from proagent_os.auth import TenantDep
from proagent_os.models.schemas import KbIngestText, KbIngestUrl, KbSearchQuery
from proagent_os.professional.kb.embeddings import EmbeddingsError
from proagent_os.services import get_services

router = APIRouter()


def _embed_unavailable(exc: EmbeddingsError) -> HTTPException:
    """Surface a missing key or a failed embeddings call instead of a bare 500."""
    return HTTPException(503, str(exc))


@router.get("/documents")
def list_docs(tenant: TenantDep) -> list:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    return get_services().kb.list_documents(tenant["id"])


@router.post("/ingest/url")
async def ingest_url(body: KbIngestUrl, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    try:
        return await get_services().kb.ingest_url(tenant["id"], body.url)
    except EmbeddingsError as exc:
        raise _embed_unavailable(exc) from exc


@router.post("/ingest/text")
def ingest_text(body: KbIngestText, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    try:
        return get_services().kb.ingest_text(tenant["id"], body.source, body.title, body.content)
    except EmbeddingsError as exc:
        raise _embed_unavailable(exc) from exc


@router.post("/ingest/file")
async def ingest_file(tenant: TenantDep, file: UploadFile = File(...)) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    suffix = Path(file.filename or "upload.txt").suffix or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        data = await file.read()
        tmp.write(data)
        path = Path(tmp.name)
    try:
        # Copy into data/uploads for sandboxed re-read if needed
        dest_dir = Path("data/uploads")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{tenant['id']}_{file.filename or path.name}"
        dest.write_bytes(data)
        try:
            return get_services().kb.ingest_file(tenant["id"], dest)
        except EmbeddingsError as exc:
            raise _embed_unavailable(exc) from exc
    finally:
        path.unlink(missing_ok=True)


@router.post("/search")
def search(body: KbSearchQuery, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    try:
        hits = get_services().kb.search(tenant["id"], body.query, top_k=body.top_k)
    except EmbeddingsError as exc:
        raise _embed_unavailable(exc) from exc
    return {"query": body.query, "hits": hits}

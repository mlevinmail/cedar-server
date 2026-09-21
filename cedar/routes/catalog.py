"""The book store: browse, search, covers, and add-to-library.

When the server has no corpus on disk, /api/catalog reports
{"available": false} and the app simply doesn't show a store.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import catalog

router = APIRouter(prefix="/api/catalog")


@router.get("")
def home():
    return catalog.home()


@router.get("/genres")
def genres():
    return {"genres": catalog.genres()}


@router.get("/search")
def search(
    q: str = Query("", max_length=200),
    genre: str = Query("", max_length=40),
    language: str = Query("", max_length=8),
    page: int = Query(0, ge=0),
):
    return catalog.search(q=q, genre=genre, language=language, page=page)


@router.get("/books/{gid}")
def book(gid: int):
    b = catalog.book(gid)
    if not b:
        raise HTTPException(404, "Book not found.")
    b["in_library"] = catalog.existing_doc(gid)
    return b


@router.get("/books/{gid}/cover")
def cover(gid: int):
    p = catalog.cover_path(gid)
    if not p:
        raise HTTPException(404, "No cover for this book.")
    # Covers never change once mirrored — let clients cache them hard.
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800, immutable"})


@router.post("/books/{gid}/add")
def add(gid: int):
    doc = catalog.add_to_library(gid)
    if not doc:
        raise HTTPException(404, "This book isn't available on this server.")
    return doc

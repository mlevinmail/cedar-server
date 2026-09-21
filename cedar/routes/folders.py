"""Library folders: one flat level of named folders.

Documents point at a folder via documents.folder_id (NULL = library root);
moving a document lives on the document route (PATCH /api/documents/{id}).
Deleting a folder never deletes reading material — its documents just return
to the root.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api")

_NAME_MAX = 60


class FolderIn(BaseModel):
    name: str


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise HTTPException(400, "Give the folder a name.")
    return name[:_NAME_MAX]


@router.get("/folders")
def list_folders():
    return {"folders": db.list_folders()}


@router.post("/folders")
def create_folder(body: FolderIn):
    return db.create_folder(_clean_name(body.name))


@router.patch("/folders/{folder_id}")
def rename_folder(folder_id: int, body: FolderIn):
    if not db.rename_folder(folder_id, _clean_name(body.name)):
        raise HTTPException(404, "Folder not found.")
    return {"ok": True}


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int):
    if not db.delete_folder(folder_id):
        raise HTTPException(404, "Folder not found.")
    return {"ok": True}

from conftest import AUTH

TEXT = ("Chapter One. It was a bright cold day in April, and the clocks were "
        "striking thirteen. Winston Smith slipped quickly through the glass doors. "
        "The hallway smelt of boiled cabbage and old rag mats.")


def _import(client, text=TEXT, title="Test"):
    r = client.post("/api/documents/from_text", headers=AUTH,
                    json={"text": text, "title": title})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_read_progress_delete(client):
    doc = _import(client)
    assert doc["num_sentences"] >= 3
    did = doc["id"]

    listed = client.get("/api/documents", headers=AUTH).json()["documents"]
    assert any(d["id"] == did for d in listed)

    detail = client.get(f"/api/documents/{did}", headers=AUTH).json()
    assert detail["title"] == "Test"
    assert detail["lang"] == "en"
    assert detail["voice"] == "af_heart"
    assert detail["voice_locked"] is False

    sents = client.get(f"/api/documents/{did}/sentences?start=0&limit=10", headers=AUTH).json()
    assert sents["sentences"][0]["idx"] == 0

    clip = client.get(f"/api/documents/{did}/tts/0?voice=af_heart&speed=1.0", headers=AUTH).json()
    assert clip["format"] == "mp3" and clip["words"]

    assert client.post(f"/api/documents/{did}/progress", headers=AUTH,
                       json={"idx": 2}).status_code == 200
    assert client.get(f"/api/documents/{did}", headers=AUTH).json()["current_idx"] == 2

    assert client.delete(f"/api/documents/{did}", headers=AUTH).status_code == 200
    assert client.get(f"/api/documents/{did}", headers=AUTH).status_code == 404


def test_empty_text_rejected(client):
    r = client.post("/api/documents/from_text", headers=AUTH, json={"text": " "})
    assert r.status_code == 400


def test_bookmarks_and_highlights(client):
    did = _import(client)["id"]
    r = client.post(f"/api/documents/{did}/bookmarks", headers=AUTH, json={"idx": 1, "note": "here"})
    assert r.status_code == 200 and r.json()["bookmark"]["note"] == "here"
    assert len(client.get(f"/api/documents/{did}/bookmarks", headers=AUTH).json()["bookmarks"]) == 1
    assert client.delete(f"/api/documents/{did}/bookmarks/1", headers=AUTH).status_code == 200

    r = client.post(f"/api/documents/{did}/highlights", headers=AUTH,
                    json={"idx": 0, "cs": 0, "ce": 7})
    assert r.status_code == 200
    hl = r.json()["highlight"]
    assert hl["text"] == "Chapter"
    # Same span again only recolours — still one row.
    client.post(f"/api/documents/{did}/highlights", headers=AUTH,
                json={"idx": 0, "cs": 0, "ce": 7, "color": "green"})
    hls = client.get(f"/api/documents/{did}/highlights", headers=AUTH).json()["highlights"]
    assert len(hls) == 1 and hls[0]["color"] == "green"
    assert client.delete(f"/api/documents/{did}/highlights/{hl['id']}", headers=AUTH).status_code == 200
    # An empty span is refused.
    assert client.post(f"/api/documents/{did}/highlights", headers=AUTH,
                       json={"idx": 0, "cs": 3, "ce": 3}).status_code == 400


def test_folders_rename_move(client):
    did = _import(client)["id"]
    f = client.post("/api/folders", headers=AUTH, json={"name": "  School  "}).json()
    assert f["name"] == "School"
    assert client.patch(f"/api/documents/{did}", headers=AUTH,
                        json={"folder_id": f["id"], "title": "Renamed"}).status_code == 200
    folders = client.get("/api/folders", headers=AUTH).json()["folders"]
    assert next(x for x in folders if x["id"] == f["id"])["count"] == 1
    assert client.get(f"/api/documents/{did}", headers=AUTH).json()["title"] == "Renamed"
    # Moving into a folder that doesn't exist is a 404, not a silent no-op.
    assert client.patch(f"/api/documents/{did}", headers=AUTH,
                        json={"folder_id": 999999}).status_code == 404
    # Deleting the folder returns the document to the root.
    assert client.delete(f"/api/folders/{f['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/api/documents/{did}", headers=AUTH).json()["folder_id"] is None


def test_catalog_absent_is_not_an_error(client):
    r = client.get("/api/catalog", headers=AUTH)
    assert r.status_code == 200 and r.json()["available"] is False
    assert client.get("/api/catalog/books/1342", headers=AUTH).status_code == 404

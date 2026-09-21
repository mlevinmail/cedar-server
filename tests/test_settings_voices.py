from conftest import AUTH

# Long enough for the detector to be sure: it refuses to guess on a few words,
# and an unplaced document follows the last voice picked, whatever language.
ENGLISH = (
    "It was the best of times, it was the worst of times, it was the age of "
    "wisdom, it was the age of foolishness, it was the epoch of belief, it was "
    "the epoch of incredulity, it was the season of Light, it was the season of "
    "Darkness, it was the spring of hope, it was the winter of despair, we had "
    "everything before us, we had nothing before us, we were all going direct "
    "to Heaven, we were all going direct the other way. There were a king with "
    "a large jaw and a queen with a plain face, on the throne of England; there "
    "were a king with a large jaw and a queen with a fair face, on the throne "
    "of France. In both countries it was clearer than crystal to the lords of "
    "the State preserves of loaves and fishes, that things in general were "
    "settled for ever."
)
SPANISH = (
    "Era el mejor de los tiempos, era el peor de los tiempos, la edad de la "
    "sabiduría, y también de la locura; la época de las creencias y de la "
    "incredulidad; la era de la luz y de las tinieblas; la primavera de la "
    "esperanza y el invierno de la desesperación. Todo lo poseíamos, pero no "
    "teníamos nada; caminábamos en derechura al cielo y nos extraviábamos por "
    "el camino opuesto. En una palabra, aquella época era tan parecida a la "
    "actual, que nuestras más notables autoridades insisten en que, tanto en "
    "lo que se refiere al bien como al mal, sólo es aceptable la comparación "
    "en grado superlativo. En el trono de Inglaterra había un rey de mandíbula "
    "muy desarrollada y una reina de cara vulgar; en el de Francia, un rey de "
    "gran quijada y una reina de hermoso rostro. En ambos países era más claro "
    "que el cristal para los señores del Estado que las cosas, en general, "
    "estaban aseguradas para siempre."
)


def test_voice_per_language(client):
    en = client.post("/api/documents/from_text", headers=AUTH,
                     json={"text": ENGLISH, "title": "English"}).json()["id"]
    assert client.get(f"/api/documents/{en}", headers=AUTH).json()["lang"] == "en"
    es = client.post("/api/documents/from_text", headers=AUTH,
                     json={"text": SPANISH, "title": "Español"}).json()["id"]
    assert client.get(f"/api/documents/{es}", headers=AUTH).json()["lang"] == "es"

    # Picking a Spanish voice on the Spanish book is a language-wide preference…
    r = client.post(f"/api/documents/{es}/voice", headers=AUTH, json={"voice": "ef_dora"}).json()
    assert r["scope"] == "language" and r["lang"] == "es"
    assert client.get("/api/settings", headers=AUTH).json()["voices"]["es"] == "ef_dora"
    # …that leaves English exactly where it was.
    assert client.get(f"/api/documents/{en}", headers=AUTH).json()["voice"] == "af_heart"

    # A cross-language pick pins only that one document.
    r = client.post(f"/api/documents/{en}/voice", headers=AUTH, json={"voice": "ff_siwis"}).json()
    assert r["scope"] == "document"
    d = client.get(f"/api/documents/{en}", headers=AUTH).json()
    assert d["voice"] == "ff_siwis" and d["voice_locked"] is True

    assert client.post(f"/api/documents/{en}/voice", headers=AUTH,
                       json={"voice": "not_a_voice"}).status_code == 400


def test_prefs_sync(client):
    r = client.put("/api/settings", headers=AUTH, json={"speed": 1.4, "theme": "sepia"}).json()
    assert r["speed"] == 1.4 and r["theme"] == "sepia"
    assert client.get("/api/settings", headers=AUTH).json()["speed"] == 1.4
    # Speed is clamped to what the player can do.
    assert client.put("/api/settings", headers=AUTH, json={"speed": 9}).json()["speed"] == 3.0


def test_server_knobs(client):
    assert client.get("/api/settings/server", headers=AUTH).json()["catalog_full"] is False
    r = client.put("/api/settings/server", headers=AUTH, json={"catalog_full": True})
    assert r.status_code == 200 and r.json()["catalog_full"] is True
    assert client.put("/api/settings/server", headers=AUTH,
                      json={"not_a_knob": 1}).status_code == 400
    client.put("/api/settings/server", headers=AUTH, json={"catalog_full": None})
    assert client.get("/api/settings/server", headers=AUTH).json()["catalog_full"] is False


def test_voices_and_live(client):
    v = client.get("/api/voices", headers=AUTH).json()
    assert "af_heart" in v["voices"] and v["defaults"]["en"]
    page = client.post("/api/live/sentences", headers=AUTH,
                       json={"text": SPANISH}).json()
    assert page["lang"] == "es" and page["voice"] == "ef_dora" and len(page["sentences"]) >= 2
    clip = client.post("/api/live/tts", headers=AUTH,
                       json={"text": page["sentences"][0], "voice": page["voice"]}).json()
    assert clip["format"] == "mp3"

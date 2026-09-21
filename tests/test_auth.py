from conftest import AUTH, KEY


def test_health_is_open(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["server"] == "cedar-server"
    assert body["mode"] == "single-user"
    assert body["kokoro_ok"] is True


def test_everything_else_needs_the_key(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_key_accepted_as_bearer_and_header(client):
    assert client.get("/api/me", headers=AUTH).status_code == 200
    assert client.get("/api/me", headers={"X-Cedar-Key": KEY}).status_code == 200


def test_me_describes_the_owner(client):
    me = client.get("/api/me", headers=AUTH).json()
    assert me["tier"] == "owner"
    assert me["plan_is_paid"] is False
    assert me["email_verified"] is True
    assert me["server"]["kind"] == "cedar-server"


def test_status_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Use your own server" in r.text

"""What someone who finds the port gets: as little as possible."""
from __future__ import annotations

import asyncio
import http.server
import io
import os
import tempfile
import threading
import zipfile

import pytest
from conftest import AUTH

from cedar import auth, bodylimit, dictionary, safefetch, tts
from cedar.chunker import extract_epub
from cedar.routes import system
from cedar.safefetch import UnsafeUrlError
from cedar.textsafe import printable

WRONG = {"Authorization": "Bearer nope"}


# ------------------------------------------------------------------- the gate

def test_schema_and_docs_are_not_published(client):
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json", headers=AUTH).status_code == 404


def test_unknown_paths_need_the_key_too(client):
    assert client.get("/wp-login.php").status_code == 401
    assert client.get("/wp-login.php", headers=AUTH).status_code == 404


def test_the_gate_reads_the_path_the_router_reads(client, monkeypatch):
    # request.url is rebuilt from the Host header, which the peer writes. On a
    # Starlette without the Host guard, "Host: x/api/health?" made url.path the
    # open health path while the router served the real one. Take the guard
    # away and the gate must still hold.
    import re

    import starlette.datastructures as ds
    if hasattr(ds, "_HOST_RE"):
        monkeypatch.setattr(ds, "_HOST_RE", re.compile(r".*", re.DOTALL))
    try:
        for host in ("x/api/health?", "x/?", "x/api/health#"):
            for path in ("/api/me", "/api/documents", "/api/settings/server"):
                r = client.get(path, headers={"Host": host})
                assert r.status_code == 401, (host, path, r.status_code)
    finally:
        auth._failures.reset("testclient")


def test_a_non_ascii_key_is_just_wrong(client):
    # Raw bytes: the test client refuses to encode a non-ASCII str, but a real
    # peer can send any byte it likes and the server decodes it as latin-1.
    accent = bytes([0xE9])
    assert client.get("/api/me", headers={b"Authorization": b"Bearer caf" + accent}).status_code == 401
    assert client.get("/api/me", headers={b"X-Cedar-Key": accent * 20}).status_code == 401


def test_throttle_never_locks_out_the_owner(client):
    peer = "testclient"  # every TestClient request comes from this address
    auth._failures.reset(peer)
    try:
        for _ in range(auth._failures.limit):
            assert client.get("/api/me", headers=WRONG).status_code == 401
        r = client.get("/api/me", headers=WRONG)
        assert r.status_code == 429 and r.headers.get("retry-after")
        # Same address, right key: straight through.
        assert client.get("/api/me", headers=AUTH).status_code == 200
        assert client.get("/api/documents", headers=AUTH).status_code == 200
    finally:
        auth._failures.reset(peer)


def test_weak_configured_key_refuses_to_start(monkeypatch):
    monkeypatch.setattr(auth, "CEDAR_KEY", "short")
    with pytest.raises(auth.WeakKeyError):
        auth.load_key()
    monkeypatch.setattr(auth, "CEDAR_KEY", "has a space in it!")
    with pytest.raises(auth.WeakKeyError):
        auth.load_key()


# ------------------------------------------------------------- body ceilings

def test_declared_oversized_body_is_refused_before_reading(client):
    r = client.post("/api/documents/from_text",
                    headers={**AUTH, "Content-Length": str(10**12),
                             "Content-Type": "application/json"},
                    content=b"{}")
    assert r.status_code == 413


def test_streamed_oversized_body_is_cut_off(client):
    limit = bodylimit.limit_for({"method": "POST", "path": "/api/documents/from_text"})
    chunk = b"x" * (1024 * 1024)

    def body():
        for _ in range(limit // len(chunk) + 2):
            yield chunk

    r = client.post("/api/documents/from_text",
                    headers={**AUTH, "Content-Type": "application/json"}, content=body())
    assert r.status_code == 413


def test_upload_route_gets_the_upload_limit():
    assert bodylimit.limit_for({"method": "POST", "path": "/api/documents"}) > \
        bodylimit.limit_for({"method": "POST", "path": "/api/documents/from_text"})


# --------------------------------------------------------- response hygiene

def test_status_page_escapes_the_host_header(client):
    r = client.get("/", headers={"Host": "<img src=x onerror=alert(1)>"})
    assert r.status_code == 200
    assert "<img" not in r.text and "&lt;img" in r.text


def test_api_responses_are_not_sniffable_or_cacheable(client):
    r = client.get("/api/me", headers=AUTH)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/").headers["content-security-policy"].startswith("default-src 'none'")


def test_health_probe_is_cached(client, monkeypatch):
    calls = []

    async def probe():
        calls.append(1)
        return True

    monkeypatch.setattr(tts, "health", probe)
    system._health = (0.0, False)
    for _ in range(3):
        assert client.get("/api/health").json()["kokoro_ok"] is True
    assert len(calls) == 1


def test_log_lines_cannot_be_forged(client, caplog):
    assert printable("ok\nFAKE 200 OK\r\x1b[0m") == "ok FAKE 200 OK  [0m"
    with caplog.at_level("INFO", logger="cedar.client"):
        client.post("/api/client_event", headers=AUTH,
                    json={"kind": "x", "detail": "ok\n2026 INFO forged line"})
    assert "forged line" in caplog.text and "\n2026 INFO" not in caplog.text


# ---------------------------------------------------------------- voice ids

def test_voice_ids_are_validated(client):
    did = client.post("/api/documents/from_text", headers=AUTH,
                      json={"text": "One two three. Four five six."}).json()["id"]
    assert client.get(f"/api/documents/{did}/tts/0?voice=../../x", headers=AUTH).status_code == 400
    # A voice mix, with its "+" encoded as a query string requires.
    assert client.get(f"/api/documents/{did}/tts/0?voice=af_bella(2)%2Baf_sky", headers=AUTH).status_code == 200
    assert client.post("/api/live/tts", headers=AUTH, json={"text": "hi", "voice": "x y"}).status_code == 400
    assert client.put("/api/settings", headers=AUTH, json={"voice": "<script>"}).status_code == 400
    assert client.post(f"/api/documents/{did}/generated", headers=AUTH,
                       json={"voice": "a" * 81}).status_code == 400
    client.delete(f"/api/documents/{did}", headers=AUTH)


# -------------------------------------------------------- import from link

@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "::ffff:127.0.0.1", "fe80::1", "fc00::1",
    "localhost", "2130706433", "fec0::1",
])
def test_private_targets_are_refused(host):
    with pytest.raises(UnsafeUrlError):
        safefetch._validate_host(host)


@pytest.mark.parametrize("host", [
    "0177.0.0.1", "012.0.0.1", "0144.0100.0.1", "127.1", "2130706433",
    "0x7f.0.0.1", "0x7f000001", "017700000001", "0177.0.0.1.",
])
def test_legacy_ipv4_spellings_are_refused_before_any_lookup(host, monkeypatch):
    # macOS reads 0177.0.0.1 as 177.0.0.1 (public) and curl reads it as
    # 127.0.0.1: the check must not depend on which resolver it runs under.
    def no_lookup(*a, **k):
        raise AssertionError("resolved a numeric host")
    monkeypatch.setattr(safefetch.socket, "getaddrinfo", no_lookup)
    with pytest.raises(UnsafeUrlError):
        safefetch._validate_host(host)


def test_a_name_with_digits_is_still_a_name(monkeypatch):
    monkeypatch.setattr(safefetch.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert safefetch._validate_host("1.example.com") == ["93.184.216.34"]
    assert safefetch._validate_host("0177.example") == ["93.184.216.34"]


def test_pin_ties_host_and_port_to_the_validated_addresses():
    from urllib.parse import urlparse
    assert safefetch._pin(urlparse("https://example.com/x"), ["93.184.216.34", "2606:2800::1"]) == \
        ["example.com:443:93.184.216.34,[2606:2800::1]"]
    assert safefetch._pin(urlparse("http://example.com:8080/"), ["1.2.3.4"]) == ["example.com:8080:1.2.3.4"]
    assert safefetch._pin(urlparse("http://1.2.3.4/"), []) == []


class _Server:
    """A local HTTP server for the pinning tests: `/` answers, `/leave`
    redirects to a loopback IP literal."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                if inner.path == "/leave":
                    inner.send_response(302)
                    inner.send_header("Location", f"http://127.0.0.1:{self.port}/secret")
                    inner.end_headers()
                    return
                body = b"pinned"
                inner.send_response(200)
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            def log_message(inner, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_fetch_connects_where_the_check_looked():
    srv = _Server()
    try:
        # pinned.invalid resolves nowhere; the pin is the only way this connects.
        hop = safefetch._browser_get_sync(f"http://pinned.invalid:{srv.port}/",
                                          pin=[f"pinned.invalid:{srv.port}:127.0.0.1"])
        assert hop.status == 200 and hop.content == b"pinned"
    finally:
        srv.close()


def test_redirect_to_a_private_address_is_refused(monkeypatch):
    srv = _Server()
    real = safefetch._validate_host

    def allow_local_name(host):
        return ["127.0.0.1"] if host == "pinned.invalid" else real(host)

    monkeypatch.setattr(safefetch, "_validate_host", allow_local_name)
    try:
        with pytest.raises(UnsafeUrlError):
            asyncio.run(safefetch.safe_fetch(f"http://pinned.invalid:{srv.port}/leave"))
    finally:
        srv.close()


# ------------------------------------------------------------------- parsers

def test_epub_xml_cannot_read_server_files():
    secret = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt")
    secret.write("TOPSECRET-4242")
    secret.close()
    opf = (f'<?xml version="1.0"?>\n<!DOCTYPE package [<!ENTITY xxe SYSTEM "file://{secret.name}">]>\n'
           '<package xmlns="http://www.idpf.org/2007/opf">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&xxe;</dc:title></metadata>'
           '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="content.opf"/></rootfiles></container>')
        z.writestr("content.opf", opf)
        z.writestr("c1.xhtml", "<html><body><p>A chapter with enough words to count as text.</p></body></html>")
    buf.seek(0)
    try:
        try:
            result = extract_epub(buf, "fallback")
        except Exception:
            return  # refusing the file outright is fine too
        assert "TOPSECRET" not in result.title
    finally:
        os.unlink(secret.name)


def test_dictionary_word_is_url_encoded(monkeypatch):
    seen = {}

    class NotFound:
        status_code = 404

    async def fake_get(url, *a, **k):
        seen["url"] = url
        return NotFound()

    monkeypatch.setattr(dictionary._client, "get", fake_get)
    asyncio.run(dictionary._wiktionary("a/b?c=d", "en", 1.0))
    assert seen["url"].endswith("/definition/a%2Fb%3Fc%3Dd")

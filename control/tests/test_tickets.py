"""HMAC media tickets: issue/verify, the ticket route, and Range on a token-gated proxy."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from volt import serve, tickets
from volt.store import Store

from tests.test_media import BLOB, TOKEN
from tests.test_operator_api import Api


SECRET = "s" * 24
NOW = 1_700_000_000


class IssueVerify(unittest.TestCase):
    def test_issue_token_is_exp_dot_hexhmac_and_verify_returns_the_asset(self):
        token, exp = tickets.issue(SECRET, "asset-a", "proxy", ttl=120, now=NOW)
        self.assertEqual(exp, NOW + 120)
        self.assertRegex(token, rf"^{exp}\.[0-9a-f]{{64}}$")
        self.assertEqual(tickets.verify(SECRET, "asset-a", "proxy", token, now=NOW), "asset-a")

    def test_expired_ticket_is_ticket_expired(self):
        token, exp = tickets.issue(SECRET, "asset-a", "proxy", ttl=10, now=NOW)
        with self.assertRaises(tickets.TicketError) as cm:
            tickets.verify(SECRET, "asset-a", "proxy", token, now=exp)
        self.assertEqual(cm.exception.code, "ticket_expired")

    def test_wrong_asset_kind_or_secret_is_ticket_invalid(self):
        token, _ = tickets.issue(SECRET, "asset-a", "proxy", ttl=120, now=NOW)
        cases = [
            ("asset-b", "proxy", SECRET, token),
            ("asset-a", "thumb", SECRET, token),
            ("asset-a", "proxy", "other-secret", token),
            ("asset-a", "proxy", SECRET, token[:-1] + ("0" if token[-1] != "0" else "1")),
            ("asset-a", "proxy", SECRET, "not-a-ticket"),
            ("asset-a", "proxy", SECRET, ""),
        ]
        for asset, kind, secret, given in cases:
            with self.subTest(asset=asset, kind=kind, given=given[:20]):
                with self.assertRaises(tickets.TicketError) as cm:
                    tickets.verify(secret, asset, kind, given, now=NOW)
                self.assertEqual(cm.exception.code, "ticket_invalid")


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = str(self.dir / "volt.db")
        self.store = Store(self.path)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.OUT_DIR)
        serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.OUT_DIR = self.path, TOKEN, None, self.dir / "out"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        a = self.store.mint_asset(owner="desk", camera="iso-3")
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"),
                               frame_rate="25", timecode_start="01:00:00:00", duration_frames=75)
        self.proxy_path = self.dir / f"{a.id}.mp4"
        self.proxy_path.write_bytes(BLOB)
        self.store.add_essence(a.id, role="proxy", location=str(self.proxy_path))
        self.a = a
        other = self.store.mint_asset(owner="desk")
        self.other = other

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.OUT_DIR = self._saved
        self.store.close()
        self.tmp.cleanup()

    def get(self, path, headers=None, token=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        try:
            conn.request("GET", path, headers=h)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def test_media_ticket_is_token_gated_and_returns_url_plus_expires(self):
        s, _, body = self.get("/api/media-ticket?asset=" + self.a.id + "&kind=proxy")
        self.assertEqual(s, 401)
        s, _, body = self.get("/api/media-ticket?asset=" + self.a.id + "&kind=proxy", token=TOKEN)
        self.assertEqual(s, 200)
        payload = json.loads(body)
        self.assertIn("url", payload)
        self.assertIn("expires", payload)
        self.assertIn(f"/api/assets/{self.a.id}/proxy?ticket=", payload["url"])
        self.assertGreater(payload["expires"], int(time.time()))

    def test_good_ticket_ranges_without_bearer(self):
        s, _, body = self.get("/api/media-ticket?asset=" + self.a.id + "&kind=proxy", token=TOKEN)
        url = json.loads(body)["url"]
        s, h, body = self.get(url, headers={"Range": "bytes=0-9"})
        self.assertEqual((s, body), (206, BLOB[:10]))
        self.assertEqual(h["content-range"], "bytes 0-9/1024")
        s, _, body = self.get(url)
        self.assertEqual((s, body), (200, BLOB))

    def test_expired_ticket_is_401_with_empty_body(self):
        token, _ = tickets.issue(TOKEN, self.a.id, "proxy", ttl=10, now=NOW)
        with mock.patch("time.time", return_value=float(NOW + 11)):
            s, _, body = self.get(f"/api/assets/{self.a.id}/proxy?ticket={token}")
        self.assertEqual(s, 401)
        self.assertEqual(body, b"")

    def test_ticket_for_another_asset_is_401_with_empty_body(self):
        token, _ = tickets.issue(TOKEN, self.a.id, "proxy", ttl=120, now=int(time.time()))
        s, _, body = self.get(f"/api/assets/{self.other.id}/proxy?ticket={token}")
        self.assertEqual(s, 401)
        self.assertEqual(body, b"")

    def test_no_ticket_and_no_bearer_is_401(self):
        s, _, body = self.get(f"/api/assets/{self.a.id}/proxy")
        self.assertEqual(s, 401)
        self.assertTrue(body)  # JSON refusal, not the empty ticket body

    def test_bearer_still_serves_without_a_ticket(self):
        s, _, body = self.get(f"/api/assets/{self.a.id}/proxy", token=TOKEN)
        self.assertEqual((s, body), (200, BLOB))

    def test_wrong_kind_on_thumb_is_401_empty(self):
        from volt import shots
        span = shots.create(self.store, self.a.id, "shot", "01:00:00:00", "01:00:01:00")["id"]
        token, _ = tickets.issue(TOKEN, self.a.id, "proxy", ttl=120, now=int(time.time()))
        s, _, body = self.get(f"/api/spans/{span}/thumb?ticket={token}")
        self.assertEqual((s, body), (401, b""))
        thumb, _ = tickets.issue(TOKEN, self.a.id, "thumb", ttl=120, now=int(time.time()))
        s, _, body = self.get(f"/api/spans/{span}/thumb?ticket={thumb}")
        self.assertNotEqual(s, 401)  # gate passed; missing jpeg is a later refusal

    def test_media_ticket_without_a_secret_is_503(self):
        serve.TOKEN = None
        serve.MEDIA_KEY = None
        s, _, body = self.get("/api/media-ticket?asset=" + self.a.id + "&kind=proxy")
        self.assertEqual(s, 503)
        self.assertEqual(json.loads(body)["error"]["code"], "no_media_key")

    def test_loopback_media_without_token_still_serves(self):
        serve.TOKEN = None
        serve.MEDIA_KEY = None
        s, _, body = self.get(f"/api/assets/{self.a.id}/proxy")
        self.assertEqual((s, body), (200, BLOB))

    def test_ttl_defaults_to_120_and_caps_at_3600(self):
        with mock.patch("time.time", return_value=float(NOW)):
            s, _, body = self.get("/api/media-ticket?asset=" + self.a.id + "&kind=proxy", token=TOKEN)
            self.assertEqual(json.loads(body)["expires"], NOW + 120)
            s, _, body = self.get(
                "/api/media-ticket?asset=" + self.a.id + "&kind=proxy&ttl=99999", token=TOKEN)
            self.assertEqual(json.loads(body)["expires"], NOW + 3600)

    def test_ticket_is_not_enough_for_post(self):
        token, _ = tickets.issue(TOKEN, self.a.id, "proxy", ttl=120, now=int(time.time()))
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        try:
            conn.request("POST", f"/api/assets/{self.a.id}/cleared?ticket={token}",
                         body=b"{}", headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = resp.read()
            self.assertEqual(resp.status, 401)
            self.assertIn(b"unauthorized", payload)
        finally:
            conn.close()


class MediaTicketApi(Api):
    def test_media_ticket_needs_the_data_token(self):
        serve.TOKEN = TOKEN
        a = self.store.mint_asset(owner="desk")
        s, p, _ = self.call("GET", f"/api/media-ticket?asset={a.id}&kind=proxy")
        self.assertEqual(s, 401)
        s, p, _ = self.call("GET", f"/api/media-ticket?asset={a.id}&kind=proxy", token=TOKEN)
        self.assertEqual(s, 200)
        self.assertTrue(p["url"].startswith(f"/api/assets/{a.id}/proxy?ticket="))

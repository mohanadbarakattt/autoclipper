"""The dashboard has no password, so web pages open in a browser must not be able to use it.

Run from the repo folder:  python -m unittest discover -s tests
"""
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(tempfile.mkdtemp(prefix="clipper-test-"))  # never the real data folder
os.environ["CLIPPER_ROOT"] = str(ROOT)
os.environ["DASHBOARD_HOST"] = "engine.example.org"
os.environ["DASHBOARD_ALLOWED_HOSTS"] = "Clips.Example.com, my-pc.tail1234.ts.net"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from clipper import config, db  # noqa: E402
from clipper.dashboard import security  # noqa: E402
from clipper.dashboard.app import EVENTS, app  # noqa: E402

logging.getLogger("dashboard").setLevel(logging.ERROR)  # every refusal logs a warning

# the way a private add-on (AutoClipper Pro) mounts its own routes on the same app after import
extra = APIRouter(prefix="/api/extra")
extra.add_api_route("/ping", lambda: {"ok": True}, methods=["GET", "POST"])
app.include_router(extra)
(ROOT / "extra-static").mkdir()
(ROOT / "extra-static" / "a.txt").write_text("hi")
app.mount("/extra-static", StaticFiles(directory=str(ROOT / "extra-static")), name="extra-static")

LOCAL = "localhost:8090"
EVIL = "https://evil.example"
# what a browser attaches to a request made by another website
CROSS_SITE = {"origin": EVIL, "sec-fetch-site": "cross-site"}


def tearDownModule():
    db.conn().close()
    shutil.rmtree(ROOT, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        config.STREAMERS_FILE.write_text(json.dumps({"streamers": [{"platform": "twitch", "channel": "victim"}]}))
        db.x("DELETE FROM candidates")
        raw = config.CANDIDATES_DIR / "raw.mp4"
        raw.write_bytes(b"x")
        self.cid = db.add_candidate({"id": "twitch:victim", "platform": "twitch", "profile": "default"},
                                    "vod", "u", str(raw), "t", 0)
        db.update_candidate(self.cid, status="failed", error="boom")
        EVENTS["vod_scan"] = self.scan = threading.Event()
        self.client = TestClient(app, base_url=f"http://{LOCAL}")

    def state(self):
        clip = db.q("SELECT status, post_url FROM candidates WHERE id=?", (self.cid,))[0]
        return (config.load_streamers(), clip, self.scan.is_set(), sorted(config.list_profiles()))

    def bodyless_posts(self):
        return ["/api/streamers/twitch:victim/toggle", "/api/scan-vods", f"/api/clips/{self.cid}/requeue",
                f"/api/clips/{self.cid}/approve", f"/api/clips/{self.cid}/posted"]

    def assert_blocked(self, method, url, **kw):
        before = self.state()
        r = self.client.request(method, url, **kw)
        self.assertEqual(r.status_code, 403, f"{method} {url} {kw} -> {r.status_code} {r.text}")
        self.assertEqual(self.state(), before, f"{method} {url} changed something")
        return r


class CrossSiteRequests(Base):
    def test_bodyless_posts_from_another_website(self):
        for url in self.bodyless_posts():
            with self.subTest(url):
                self.assert_blocked("POST", url, headers=CROSS_SITE)

    def test_html_form_post_from_another_website(self):
        self.assert_blocked("POST", "/api/streamers/twitch:victim/toggle",
                            headers={**CROSS_SITE, "content-type": "application/x-www-form-urlencoded"})

    def test_json_without_content_type_from_another_website(self):
        for url, body in [("/api/streamers", {"platform": "kick", "channel": "planted"}),
                          (f"/api/clips/{self.cid}/posted", {"url": f"{EVIL}/x"}),
                          ("/api/manual", {"url": f"{EVIL}/v", "start": 0, "end": 1})]:
            with self.subTest(url):
                self.assert_blocked("POST", url, headers=CROSS_SITE, content=json.dumps(body))

    def test_another_app_on_the_same_computer(self):
        self.assert_blocked("POST", "/api/scan-vods",
                            headers={"origin": "http://localhost:8000", "sec-fetch-site": "same-site"})

    def test_origin_alone_is_enough(self):
        """Browsers send no Sec-Fetch-Site to a plain http LAN address, and old ones never do."""
        for origin in (EVIL, "null", "http://localhost:8000", "http://localhost", "http://evil.localhost:8090", ""):
            with self.subTest(origin=origin):
                self.assert_blocked("POST", "/api/scan-vods", headers={"origin": origin})

    def test_fetch_metadata_alone_is_enough(self):
        for site in ("cross-site", "same-site"):
            with self.subTest(site):
                self.assert_blocked("POST", "/api/scan-vods", headers={"sec-fetch-site": site})

    def test_every_state_changing_method(self):
        for method, url in [("PATCH", "/api/streamers/twitch:victim"), ("PUT", "/api/profiles/planted"),
                            ("DELETE", "/api/streamers/twitch:victim"), ("DELETE", f"/api/clips/{self.cid}")]:
            with self.subTest(f"{method} {url}"):
                self.assert_blocked(method, url, headers=CROSS_SITE, content=json.dumps({"paused": True}))

    def test_routes_added_by_an_addon(self):
        self.assert_blocked("POST", "/api/extra/ping", headers=CROSS_SITE)


class DnsRebinding(Base):
    """A website points its own domain at 127.0.0.1; the browser then thinks the dashboard is that website."""

    def rebound(self, host="rebind.evil.example:8090"):
        return {"host": host, "origin": f"http://{host}", "sec-fetch-site": "same-origin"}

    def test_cannot_read(self):
        for url in ("/", "/api/clips", "/api/streams", "/api/overview", "/media/clip.mp4",
                    "/api/extra/ping", "/extra-static/a.txt"):
            with self.subTest(url):
                self.assert_blocked("GET", url, headers={"host": "rebind.evil.example:8090"})

    def test_cannot_write(self):
        for method, url in [("POST", "/api/scan-vods"), ("DELETE", "/api/streamers/twitch:victim"),
                            ("PUT", "/api/profiles/planted"), ("POST", "/api/extra/ping")]:
            with self.subTest(f"{method} {url}"):
                self.assert_blocked(method, url, headers=self.rebound(), content="{}")

    def test_lookalike_and_malformed_hosts(self):
        for host in ("localhost.evil.example", "127.0.0.1.evil.example", "evil.example.", "192.168.1.5.nip.io",
                     "localhost:8090@evil.example", "evil.example@localhost", "localhost:abc", "[evil.example]",
                     "[::1].evil.example", "evil.example/localhost", "localhost, evil.example", ".local", ""):
            with self.subTest(host=host):
                self.assert_blocked("GET", "/api/clips", headers={"host": host})

    def test_network_names_are_refused_on_a_this_computer_only_dashboard(self):
        """Whoever runs the Wi-Fi decides where these point; nobody needs them to reach 127.0.0.1."""
        for bound in ("127.0.0.1", "localhost", "::1"):
            with mock.patch.object(security, "DASHBOARD_HOST", bound):
                for host in ("nas:8090", "mypc.lan:8090", "printer.local:8090", "box.home.arpa", "x.internal"):
                    with self.subTest(bound=bound, host=host):
                        self.assert_blocked("GET", "/api/clips", headers={"host": host})
                for host in ("localhost:8090", "127.0.0.1:8090", "[::1]:8090", "app.localhost:8090",
                             f"{socket.gethostname()}:8090", "host.docker.internal:8090", "clips.example.com"):
                    with self.subTest(bound=bound, allowed=host):
                        self.assertEqual(self.client.get("/api/overview", headers={"host": host}).status_code, 200)

    def test_message_says_how_to_allow_a_name(self):
        r = self.assert_blocked("GET", "/", headers={"host": "box.example.net:8090"})
        self.assertIn("box.example.net", r.json()["detail"])
        self.assertIn("DASHBOARD_ALLOWED_HOSTS", r.json()["detail"])


class StillWorks(Base):
    def ok(self, method, url, **kw):
        r = self.client.request(method, url, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {url} {kw} -> {r.status_code} {r.text}")
        return r

    def test_openbot_routines(self):
        """curl and agents send neither Origin nor Sec-Fetch-Site."""
        self.ok("GET", "/api/health")
        self.ok("GET", "/api/clips?limit=100")
        self.ok("POST", "/api/streamers", json={"platform": "kick", "channel": "new", "profile": "default"})
        self.assertTrue(self.ok("POST", "/api/streamers/kick:new/toggle").json()["paused"])
        self.ok("PATCH", "/api/streamers/kick:new", json={"profile": "gaming"})
        self.ok("DELETE", "/api/streamers/kick:new")
        self.ok("GET", "/api/streams")

    def test_agent_posting(self):
        for host in (LOCAL, "127.0.0.1:8090"):
            with self.subTest(host):
                self.ok("GET", "/api/posting-queue?platform=youtube&limit=3", headers={"host": host})
                self.ok("POST", f"/api/clips/{self.cid}/posted", json={"url": "https://youtu.be/x"},
                        headers={"host": host})

    def test_docker_healthcheck(self):
        self.ok("GET", "/api/overview", headers={"host": "localhost:8090"})

    def test_the_dashboard_itself(self):
        for host in (LOCAL, "127.0.0.1:8090", "[::1]:8090"):
            with self.subTest(host):
                self.ok("GET", "/", headers={"host": host})
                self.ok("POST", "/api/scan-vods",
                        headers={"host": host, "origin": f"http://{host}", "sec-fetch-site": "same-origin"})
        self.assertTrue(self.scan.is_set())

    def test_typed_or_bookmarked_requests(self):
        self.ok("POST", "/api/scan-vods", headers={"sec-fetch-site": "none"})

    def test_opening_a_link_to_the_dashboard(self):
        self.ok("GET", "/", headers={"sec-fetch-site": "cross-site"})

    def test_lan_ip_and_host_names(self):
        me = socket.gethostname()
        for host in ("192.168.1.20:8090", "10.0.0.7:8090", "100.101.102.103:8090", "[fe80::1%25eth0]:8090",
                     "[fd00::5]:8090", f"{me}:8090", f"{me.upper()}.local:8090", "nas:8090", "mypc.lan:8090",
                     "box.home.arpa", "clipper:8090", "host.docker.internal:8090", "app.localhost:8090",
                     "localhost.:8090", "LOCALHOST:8090"):
            with self.subTest(host):
                self.ok("GET", "/api/overview", headers={"host": host})
                # plain http: browsers send Origin only
                self.ok("POST", "/api/scan-vods", headers={"host": host, "origin": f"http://{host.lower()}"})

    def test_full_computer_name(self):
        security.computer_names.cache_clear()  # looked up once per run
        self.addCleanup(security.computer_names.cache_clear)
        with mock.patch("socket.getfqdn", return_value="mypc.corp.example.net"):
            self.ok("GET", "/api/overview", headers={"host": "MyPC.corp.example.net:8090"})

    def test_names_from_settings(self):
        for host in ("clips.example.com", "my-pc.tail1234.ts.net:8090", "engine.example.org:8090"):
            with self.subTest(host):
                self.ok("POST", "/api/scan-vods", headers={"host": host, "origin": f"https://{host}"})

    def test_proxy_that_rewrites_the_host(self):
        self.ok("POST", "/api/scan-vods",
                headers={"origin": "https://clips.example.com", "sec-fetch-site": "same-origin"})

    def test_routes_added_by_an_addon(self):
        self.ok("POST", "/api/extra/ping")
        self.ok("POST", "/api/extra/ping", headers={"origin": f"http://{LOCAL}", "sec-fetch-site": "same-origin"})
        self.ok("GET", "/extra-static/a.txt")


if __name__ == "__main__":
    unittest.main()

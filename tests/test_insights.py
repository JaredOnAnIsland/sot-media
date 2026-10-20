"""insights.py with a fake Instagram API: no network, no git. Run: python -m unittest discover -s tests"""
import json, os, sys, tempfile, unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import insights  # noqa: E402

BASE = "https://graph.instagram.com/v23.0"
NEXT = f"{BASE}/U1/media?after=abc&access_token=TKN&limit=50"


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def rows(names):
    """Meta's lifetime rows; watch times come in milliseconds."""
    return {"data": [{"name": n, "period": "lifetime", "values": [{"value": 1200 if "watch" in n else 7}]}
                     for n in names]}


class FakeIG:
    """Two pages of media (one is not a Reel). Meta refuses `refused` metrics everywhere, and any metric set on
    Reel R2, so R2 falls back to one call per metric (and "saved" fails there)."""

    def __init__(self, refused=("reposts",), account_ok=True):
        self.calls, self.refused, self.account_ok = [], set(refused), account_ok

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        path = urlsplit(url).path[len("/v23.0/"):]
        p = params or {}
        if url == NEXT:
            return Resp({"data": [{"id": "R3", "media_product_type": "REELS", "permalink": "https://i/reel/R3/",
                                   "timestamp": "2026-10-01T23:00:00+0000", "caption": "third"}]})
        if path == "U1/media":
            return Resp({"data": [{"id": "R1", "media_product_type": "REELS", "permalink": "https://i/reel/R1/",
                                   "timestamp": "2099-01-01T23:00:00+0000", "caption": "newest\nline two"},
                                  {"id": "P1", "media_product_type": "FEED", "permalink": "https://i/p/P1/"},
                                  {"id": "R2", "media_product_type": "REELS", "permalink": "https://i/reel/R2/",
                                   "timestamp": "2099-01-01T22:00:00+0000", "caption": "second"}],
                         "paging": {"next": NEXT}})
        if path.endswith("/insights"):
            names = p["metric"].split(",")
            if self.refused & set(names) or (path.startswith("R2/") and len(names) > 1):
                return Resp({"error": {"message": "(#100) metric refused", "code": 100}}, 400)
            if path.startswith("R2/") and names == ["saved"]:
                return Resp({"error": {"message": "(#10) not available", "code": 10}}, 400)
            return Resp(rows(names))
        if path == "me":
            if not self.account_ok:
                return Resp({"error": {"message": "nope"}}, 400)
            return Resp({"username": "sot", "followers_count": 15, "follows_count": 3, "media_count": 40, "id": "U1"})
        return Resp({"error": {"message": f"unexpected {url}"}}, 400)


class InsightsTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(insights, TOKEN="TKN", USER="U1")
        p.start()
        self.addCleanup(p.stop)

    def build(self, fake, old=None):
        with mock.patch.object(insights.requests, "get", fake):
            return insights.build(insights.Api(), old or {})

    def test_values_reads_lifetime_and_total_value_rows(self):
        data = {"data": [{"name": "views", "values": [{"value": 42}]},
                         {"name": "reach", "total_value": {"value": 30}},
                         {"name": "likes"}]}
        self.assertEqual(insights.values(data), {"views": 42, "reach": 30, "likes": None})

    def test_build_drops_refused_metric_and_keeps_the_rest(self):
        out = self.build(FakeIG())
        self.assertEqual(out["metrics"], [m for m in insights.METRICS if m != "reposts"])
        self.assertIn("reposts", out["dropped"])
        self.assertEqual([r["id"] for r in out["reels"]], ["R1", "R2", "R3"])  # the FEED post is left out
        r1 = out["reels"][0]
        self.assertEqual(r1["views"], 7)
        self.assertEqual(r1["ig_reels_avg_watch_time"], 1200)  # milliseconds, as Meta sends them
        self.assertEqual(r1["caption"], "newest")
        self.assertNotIn("missing", r1)
        self.assertNotIn("reposts", r1)

    def test_probe_uses_a_reel_older_than_two_days(self):
        fake = FakeIG()
        self.build(fake)
        first_insights = next(u for u, _ in fake.calls if u.endswith("/insights"))
        self.assertTrue(first_insights.endswith("/R3/insights"))  # R1 and R2 are too new to test metrics on

    def test_one_refused_metric_does_not_blank_a_reel(self):
        r2 = self.build(FakeIG())["reels"][1]
        self.assertEqual(r2["missing"], ["saved"])
        self.assertIsNone(r2["saved"])
        self.assertEqual(r2["views"], 7)

    def test_account_and_followers_history(self):
        out = self.build(FakeIG(), old={"followers_history": {"2026-10-01": 12}})
        self.assertEqual(out["account"], {"username": "sot", "followers_count": 15, "follows_count": 3,
                                          "media_count": 40})
        self.assertEqual(out["followers_history"]["2026-10-01"], 12)
        self.assertIn(15, out["followers_history"].values())

    def test_account_refused_is_not_fatal(self):
        out = self.build(FakeIG(account_ok=False), old={"followers_history": {"2026-10-01": 12}})
        self.assertIn("error", out["account"])
        self.assertEqual(out["followers_history"], {"2026-10-01": 12})
        self.assertEqual(len(out["reels"]), 3)

    def test_paging_link_is_used_as_is(self):
        fake = FakeIG()
        self.build(fake)
        self.assertIn((NEXT, {}), fake.calls)

    def test_network_error_text_never_reaches_the_file(self):
        def boom(url, params=None, timeout=None):
            raise insights.requests.ConnectionError(f"Max retries exceeded with url: /x?access_token={insights.TOKEN}")
        with mock.patch.object(insights.requests, "get", boom):
            ok, err = insights.Api().get("me")
        self.assertFalse(ok)
        self.assertEqual(err, {"message": "ConnectionError"})
        self.assertEqual(insights.clean({"message": "token TKN here"}), "token *** here")

    def test_main_writes_json_without_touching_queue(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(insights.requests, "get", FakeIG()):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                open("queue.json", "w").write("[]\n")
                self.assertEqual(insights.main(), 0)
                out = json.load(open("insights.json"))
                self.assertEqual(open("queue.json").read(), "[]\n")
            finally:
                os.chdir(cwd)
        self.assertEqual(len(out["reels"]), 3)
        self.assertNotIn("TKN", json.dumps(out))

    def test_main_keeps_old_file_when_every_metric_is_refused(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(insights.requests, "get", FakeIG(refused=insights.METRICS)):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                json.dump({"metrics": ["views"], "reels": [{"id": "R1", "views": 5}]}, open("insights.json", "w"))
                before = open("insights.json").read()
                self.assertEqual(insights.main(), 1)
                self.assertEqual(open("insights.json").read(), before)
            finally:
                os.chdir(cwd)

    def test_main_without_secrets_reads_nothing(self):
        with mock.patch.multiple(insights, TOKEN="", USER=""), mock.patch.object(insights.requests, "get") as g:
            self.assertEqual(insights.main(), 1)
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()

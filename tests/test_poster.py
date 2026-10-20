"""poster.py with a fake Instagram API: no network, no git. Run: python -m unittest discover -s tests"""
import json, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import poster  # noqa: E402

BASE = "https://graph.instagram.com/v23.0"
TRIAL = '{"graduation_strategy":"SS_PERFORMANCE"}'


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeIG:
    """Records every call; refuses a container request that carries any key in `refuse`."""

    def __init__(self, refuse=()):
        self.calls, self.refuse = [], set(refuse)

    def __call__(self, method, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, url, params))
        path = url[len(BASE) + 1:]
        if method == "POST" and path == "U1/media":
            if self.refuse & set(params):
                return Resp({"error": {"message": "(#100) refused", "code": 100}}, 400)
            return Resp({"id": "C1"})
        if method == "GET" and path == "C1":
            return Resp({"status_code": "FINISHED"})
        if method == "POST" and path == "U1/media_publish":
            return Resp({"id": "M1"})
        if method == "GET" and path == "M1":
            return Resp({"permalink": "https://www.instagram.com/reel/X1/"})
        return Resp({"error": {"message": f"unexpected {method} {path}"}}, 400)

    def containers(self):
        return [p for m, u, p in self.calls if m == "POST" and u.endswith("/U1/media")]


def reel(**kw):
    e = {"id": "1", "at_epoch": 0, "caption": "cap", "status": "pending", "label": "a.mp4",
         "video_url": "https://x/a.mp4", "cover_url": "https://x/cover-a.jpg", "thumb_ms": 0, "share_to_feed": True}
    e.update(kw)
    return e


class PosterTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.multiple(poster, TOKEN="TKN", USER="U1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_publish(self, e, refuse=()):
        fake = FakeIG(refuse)
        with mock.patch.object(poster.requests, "request", fake), mock.patch.object(poster.time, "sleep"):
            link = poster.publish(e)
        return fake, link

    def test_normal_reel_sends_exactly_the_old_fields(self):
        fake, link = self.run_publish(reel())
        self.assertEqual(link, "https://www.instagram.com/reel/X1/")
        self.assertEqual(fake.containers(), [{"cover_url": "https://x/cover-a.jpg", "media_type": "REELS",
                                              "video_url": "https://x/a.mp4", "caption": "cap",
                                              "share_to_feed": "true", "access_token": "TKN"}])
        self.assertEqual([(m, u[len(BASE) + 1:]) for m, u, _ in fake.calls],
                         [("POST", "U1/media"), ("GET", "C1"), ("POST", "U1/media_publish"), ("GET", "M1")])

    def test_normal_reel_key_order_unchanged(self):
        fake, _ = self.run_publish(reel(thumb_ms=1500, share_to_feed=False))
        self.assertEqual(list(fake.containers()[0]),
                         ["cover_url", "media_type", "video_url", "caption", "share_to_feed", "thumb_offset",
                          "access_token"])
        self.assertEqual(fake.containers()[0]["thumb_offset"], "1500")
        self.assertEqual(fake.containers()[0]["share_to_feed"], "false")

    def test_normal_reel_without_cover(self):
        e = reel()
        del e["cover_url"]
        fake, _ = self.run_publish(e)
        self.assertEqual(fake.containers(), [{"media_type": "REELS", "video_url": "https://x/a.mp4", "caption": "cap",
                                              "share_to_feed": "true", "access_token": "TKN"}])

    def test_normal_reel_cover_refused_retries_without_cover(self):
        fake, link = self.run_publish(reel(), refuse={"cover_url"})
        self.assertEqual([("cover_url" in p, "trial_params" in p) for p in fake.containers()],
                         [(True, False), (False, False)])
        self.assertTrue(link.startswith("https://"))

    def test_trial_reel_adds_trial_params(self):
        e = reel(trial=True)
        fake, _ = self.run_publish(e)
        c = fake.containers()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["trial_params"], TRIAL)
        self.assertEqual(json.loads(c[0]["trial_params"]), {"graduation_strategy": "SS_PERFORMANCE"})
        self.assertEqual({k: v for k, v in c[0].items() if k != "trial_params"},
                         {"cover_url": "https://x/cover-a.jpg", "media_type": "REELS", "video_url": "https://x/a.mp4",
                          "caption": "cap", "share_to_feed": "true", "access_token": "TKN"})
        self.assertNotIn("trial_refused", e)

    def test_trial_refused_posts_normal_reel_with_cover(self):
        e = reel(trial=True)
        fake, link = self.run_publish(e, refuse={"trial_params"})
        self.assertEqual([("cover_url" in p, "trial_params" in p) for p in fake.containers()],
                         [(True, True), (False, True), (True, False)])
        self.assertIn("refused", e["trial_refused"])
        self.assertTrue(link.startswith("https://"))

    def test_trial_with_cover_refused_keeps_trial(self):
        e = reel(trial=True)
        fake, _ = self.run_publish(e, refuse={"cover_url"})
        self.assertEqual([("cover_url" in p, "trial_params" in p) for p in fake.containers()],
                         [(True, True), (False, True)])
        self.assertNotIn("trial_refused", e)

    def test_everything_refused_raises(self):
        e = reel(trial=True)
        with self.assertRaises(RuntimeError):
            self.run_publish(e, refuse={"media_type"})

    def test_carousel_ignores_trial(self):
        e = {"id": "2", "at_epoch": 0, "caption": "c", "status": "pending", "trial": True,
             "image_urls": ["https://x/1.jpg", "https://x/2.jpg"]}
        fake = FakeIG()
        with mock.patch.object(poster.requests, "request", fake), mock.patch.object(poster.time, "sleep"):
            poster.publish(e)
        self.assertFalse(any("trial_params" in p for _, _, p in fake.calls))

    def test_main_posts_due_trial_entry_and_scrubs_token(self):
        q = [reel(id="due", trial=True, at_epoch=1), reel(id="later", at_epoch=4e9),
             reel(id="broken", at_epoch=2, video_url="https://x/b.mp4")]
        fake = FakeIG()

        def flaky(method, url, params=None, timeout=None):
            if params and params.get("video_url") == "https://x/b.mp4":
                raise ConnectionError(f"Max retries exceeded with url: /media?access_token={poster.TOKEN}")
            return fake(method, url, params, timeout)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(poster.requests, "request", flaky), \
                mock.patch.object(poster.time, "sleep"), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), \
                mock.patch.object(poster, "keep_token_alive"), mock.patch.object(poster.time, "time", return_value=3600):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                json.dump(q, open("queue.json", "w"))
                poster.main()
                out = {e["id"]: e for e in json.load(open("queue.json"))}
            finally:
                os.chdir(cwd)
        self.assertEqual(out["due"]["status"], "done")
        self.assertEqual(out["due"]["result"], "https://www.instagram.com/reel/X1/")
        self.assertEqual(out["later"]["status"], "pending")
        self.assertEqual(out["broken"]["status"], "failed")
        self.assertNotIn("TKN", out["broken"]["error"])
        self.assertIn("***", out["broken"]["error"])
        self.assertEqual(sum("trial_params" in p for p in fake.containers()), 1)


if __name__ == "__main__":
    unittest.main()

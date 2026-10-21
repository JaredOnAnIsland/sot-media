"""insights.py with a fake Instagram API: no network, no git. Run: python -m unittest discover -s tests"""
import json, os, sys, tempfile, time, unittest
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


PACIFIC = insights.META_TZ
NOW = 1792582860  # 2026-10-21 11:41 UTC, when the daily job runs


def pacific_starts(since, until):
    """Meta days whose starting midnight (Pacific) is inside [since, until], as (unix start, Pacific date)."""
    import datetime as dt
    day = dt.datetime.fromtimestamp(since, PACIFIC).date()
    out = []
    while True:
        start = int(dt.datetime(day.year, day.month, day.day, tzinfo=PACIFIC).timestamp())
        if start > until:
            return out
        if start >= since:
            out.append((start, day.isoformat()))
        day += dt.timedelta(days=1)


class FakeAccountIG(FakeIG):
    """FakeIG plus account insights that behave as Meta's do (probe 2026-10-20, Sep 10 White House Reel): a
    total_value answer is the sum of the Pacific days whose STARTING midnight falls in [since, until], and a
    time_series value's end_time is that midnight; follows_and_unfollows and follower_count come back empty below
    100 followers; "impressions" is refused. Views on a day = 10 x its day of the month."""

    def __init__(self, history_from="2026-01-01", followers_100=False, **kw):
        super().__init__(**kw)
        self.history_from, self.followers_100 = history_from, followers_100

    def __call__(self, url, params=None, timeout=None):
        if urlsplit(url).path != "/v23.0/U1/insights":
            return super().__call__(url, params, timeout)
        p = dict(params or {})
        self.calls.append((url, p))
        names = p["metric"].split(",")
        if "impressions" in names:
            return Resp({"error": {"message": "(#100) metric[0] must be one of the following values", "code": 100}}, 400)
        days = [d for _, d in pacific_starts(int(p["since"]), int(p["until"]))]
        if any(d < self.history_from for d in days):
            return Resp({"error": {"message": "(#100) too old", "code": 100}}, 400)
        if names == ["follower_count"]:
            if not self.followers_100:
                return Resp({"data": []})
            return Resp({"data": [{"name": "follower_count", "period": "day", "values": [
                {"value": 2, "end_time": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(s))}
                for s, _ in pacific_starts(int(p["since"]), int(p["until"]))]}]})
        rows = []
        for n in names:
            if n == "follows_and_unfollows" and not self.followers_100:
                continue
            value = sum(int(d[-2:]) * 10 for d in days) if n == "views" else 5 if n == "follows_and_unfollows" else 1
            row = {"name": n, "period": "day", "total_value": {"value": value}}
            if p.get("breakdown") == "follow_type":
                parts = {"reach": (("NON_FOLLOWER", 96), ("FOLLOWER", 4)),
                         "follows_and_unfollows": (("FOLLOWER", 4), ("NON_FOLLOWER", 1))}[n]
                row["total_value"]["breakdowns"] = [{"dimension_keys": ["follow_type"], "results": [
                    {"dimension_values": [k], "value": v} for k, v in parts]}]
            rows.append(row)
        return Resp({"data": rows})


class InsightsTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(insights, TOKEN="TKN", USER="U1")
        p.start()
        self.addCleanup(p.stop)

    def build(self, fake, old=None, now=None):
        with mock.patch.object(insights.requests, "get", fake):
            return insights.build(insights.Api(), old or {}, now=now)

    # ---- account numbers per Meta day (added 2026-10-21) ----

    def account_calls(self, fake):
        return [p for u, p in fake.calls if u.endswith("/U1/insights")]

    def test_each_account_row_is_one_meta_day(self):
        fake = FakeAccountIG()
        out = self.build(fake, now=NOW)
        daily = out["account_daily"]
        self.assertEqual(daily["metrics"], insights.ACCOUNT_METRICS)
        self.assertEqual(daily["dropped"], {})
        row = daily["days"]["2026-10-20"]  # the newest complete Pacific day at 4:41 AM Pacific on the 21st
        self.assertEqual(row["views"], 200)  # one day's views, not two days summed
        self.assertEqual((row["reach_followers"], row["reach_non_followers"]), (4, 96))
        self.assertNotIn("follows", row)  # Meta sends no follows below 100 followers
        self.assertNotIn("2026-10-21", daily["days"])  # today is not complete yet
        for p in self.account_calls(fake):
            if p["metric"] != "follower_count":
                self.assertEqual(len(pacific_starts(p["since"], p["until"])), 1)
                self.assertEqual((p["period"], p["metric_type"]), ("day", "total_value"))

    def test_daylight_saving_day_still_holds_one_day_start(self):
        for day in ("2026-10-31", "2026-11-01", "2026-11-02", "2027-03-14"):
            p = insights.day_params(day)
            self.assertEqual([d for _, d in pacific_starts(p["since"], p["until"])], [day])

    def test_old_days_are_kept_and_the_newest_week_is_reread(self):
        old = {"account_daily": {"basis_version": 2,
                                 "days": {"2026-06-02": {"views": 1}, "2026-10-19": {"views": -1, "x": 3}}}}
        days = self.build(FakeAccountIG(), old=old, now=NOW)["account_daily"]["days"]
        self.assertEqual(days["2026-06-02"], {"views": 1})  # never dropped, never re-read
        self.assertEqual(days["2026-10-19"]["views"], 190)  # refreshed: Meta settles over 48 hours
        self.assertEqual(days["2026-10-19"]["x"], 3)  # a value Meta did not send again is kept
        self.assertEqual(list(days), sorted(days))

    def test_backfill_is_capped_and_stops_at_metas_history(self):
        out = self.build(FakeAccountIG(history_from="2026-10-01"), now=NOW)["account_daily"]
        days = out["days"]
        self.assertEqual(min(days), "2026-10-01")
        self.assertIn("2026-09-30", out["errors"])  # the first refused day ends the backfill for this run
        self.assertEqual(len(out["errors"]), 1)
        everything = self.build(FakeAccountIG(), now=NOW)["account_daily"]["days"]
        self.assertEqual(min(everything), insights.FIRST_DAY)  # one run reaches the first day
        self.assertEqual(len(everything), 142)  # 2026-06-01 to 2026-10-20
        with mock.patch.object(insights, "BACKFILL_PER_RUN", 30):
            first = self.build(FakeAccountIG(), now=NOW)["account_daily"]["days"]
            self.assertEqual(len(first), insights.REFRESH_DAYS + 30)
            later = self.build(FakeAccountIG(), old={"account_daily": {"basis_version": 2, "days": first}}, now=NOW)
        self.assertEqual(len(later["account_daily"]["days"]), insights.REFRESH_DAYS + 60)  # carries on, newest first

    def test_an_empty_answer_is_not_stored_and_ends_the_backfill(self):
        class Empty(FakeAccountIG):
            def __call__(self, url, params=None, timeout=None):
                p = params or {}
                if url.endswith("/U1/insights") and p.get("since", 0) < insights.day_start("2026-10-05") - 43200:
                    self.calls.append((url, dict(p)))
                    return Resp({"data": []})  # what Meta sends for days it has no numbers for
                return super().__call__(url, params, timeout)
        out = self.build(Empty(), now=NOW)["account_daily"]
        self.assertEqual(min(out["days"]), "2026-10-05")
        self.assertEqual(out["errors"], {"2026-10-04": "no numbers for this day"})

    def test_refused_account_metric_is_dropped_not_fatal(self):
        with mock.patch.object(insights, "ACCOUNT_METRICS", ["impressions"] + insights.ACCOUNT_METRICS):
            daily = self.build(FakeAccountIG(), now=NOW)["account_daily"]
        self.assertIn("impressions", daily["dropped"])
        self.assertNotIn("impressions", daily["metrics"])
        self.assertEqual(daily["days"]["2026-10-20"]["views"], 200)

    def test_follows_split_once_meta_sends_them(self):
        row = self.build(FakeAccountIG(followers_100=True), now=NOW)["account_daily"]["days"]["2026-10-20"]
        self.assertEqual((row["follows"], row["unfollows"]), (4, 1))
        self.assertEqual(row["follower_count"], 2)

    def test_first_run_labels_are_moved_one_day_once(self):
        old = {"account_daily": {"days": {"2026-10-18": {"views": 1003, "read": "2026-10-20"}}}}
        days = self.build(FakeAccountIG(), old=old, now=NOW)["account_daily"]
        self.assertEqual(days["basis_version"], insights.BASIS)
        self.assertEqual(days["days"]["2026-10-19"]["views"], 190)  # moved to the 19th, then re-read (newest week)
        older = {"account_daily": {"days": {"2026-08-01": {"views": 7}}}}
        moved = self.build(FakeAccountIG(), old=older, now=NOW)["account_daily"]
        self.assertEqual(moved["days"]["2026-08-02"], {"views": 7})  # an old day keeps its numbers, one day later
        again = self.build(FakeAccountIG(), old={"account_daily": moved}, now=NOW)["account_daily"]
        self.assertEqual(again["days"]["2026-08-02"], {"views": 7})  # never moved twice

    def test_a_daily_block_surprise_keeps_reels_and_old_days(self):
        old = {"account_daily": {"days": {"2026-10-01": {"views": 9}}}}
        with mock.patch.object(insights, "account_daily", side_effect=KeyError("boom")):
            out = self.build(FakeAccountIG(), old=old, now=NOW)
        self.assertEqual(len(out["reels"]), 3)
        self.assertEqual(out["account_daily"]["days"], {"2026-10-01": {"views": 9}})
        self.assertIn("KeyError", out["account_daily"]["errors"]["run"])

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

"""Read-only Reel insights for the Instagram account, run once a day by .github/workflows/insights.yml.

Reads every posted Reel's lifetime numbers, the account's follower count and the account's numbers per day,
and writes insights.json. It only READS from Instagram: no posting, no token refresh, and it never opens
queue.json. The workflow commits insights.json alone, so it cannot disturb the poster (post.yml / poster.py
commits queue.json). In the app repo, `ig_publish.py insights --cloud` turns insights.json into
marketing/social/insights.csv and insights_daily.csv.

Metrics are the Reels metrics Meta lists for the Instagram API with Instagram Login. A metric the API
refuses is left out for that run (named under "dropped"), never fatal. Watch times are milliseconds,
as Meta sends them. Meta's numbers can lag up to 48 hours and count organic activity only.
Doc: https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights

Account numbers per day (added 2026-10-21) go in "account_daily": profile views, bio-link taps, reach split
into followers and non-followers, and the rest, one row per Meta day. Old days are never dropped; the newest
week is re-read every run because Meta's numbers settle over 48 hours. Doc:
https://developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights
"""
import calendar, datetime, json, os, sys, time
from zoneinfo import ZoneInfo
import requests

BASE = "https://graph.instagram.com/v23.0"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER = os.environ.get("IG_USER_ID", "")
OUT = "insights.json"
METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
           "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate", "reposts"]
ACCOUNT_FIELDS = ["username", "followers_count", "follows_count", "media_count"]
MAX_MEDIA = 500
MAX_CALLS = 1500  # a hard stop far above a normal day (about 2 per Reel), so a bad day cannot loop

# Account metrics asked per day (period=day, metric_type=total_value). A probe on 2026-10-20 (v23.0, 14
# followers) got numbers for all of these; follows_and_unfollows and follower_count came back EMPTY, because
# Meta only sends them once an account has 100 followers, and "impressions" is refused (gone since 2025).
# Until then, daily follows come from the followers_count history below.
ACCOUNT_METRICS = ["views", "reach", "accounts_engaged", "total_interactions", "likes", "comments", "shares",
                   "saves", "reposts", "replies", "profile_views", "website_clicks", "profile_links_taps",
                   "follows_and_unfollows"]
# Meta's days run midnight to midnight US Pacific (boundaries at 07:00 UTC in summer time). A total_value answer is
# the sum of the whole days whose START (the midnight Meta labels end_time) falls between since and until: the
# 2026-10-20 probe got the same numbers for 23:00-11:00 UTC as for 23:00-23:00, and nothing for 11:00-23:00, and
# the White House Reel posted 6 AM Pacific on Sep 10 shows in the day that starts at midnight Sep 10 (1,212 views),
# not the day before (the first run, 2026-10-20, read it the other way round; BASIS 2 fixed that). Hours cannot be
# asked for; each row is one Meta day, asked with a window from 12 hours before its midnight to 12 hours after.
META_TZ = ZoneInfo("America/Los_Angeles")
BASIS = 2  # 1 = the first run's labels (each day filed one day early), moved once by account_daily()
FIRST_DAY = "2026-06-01"  # the account's first Reel posted 2026-06-03
REFRESH_DAYS = 7          # re-read this many newest days every run (Meta settles within 48 hours)
BACKFILL_PER_RUN = 150    # older missing days per run, newest first (about 2 calls a day; budget above)


class Api:
    def __init__(self):
        self.calls = 0

    def get(self, path, **params):
        """GET -> (ok, data). An API refusal or network error comes back as (False, error); never raises."""
        if self.calls >= MAX_CALLS:
            return False, {"message": "call budget used up for this run"}
        self.calls += 1
        if path.startswith("https://"):  # a paging "next" link already carries every parameter
            url, params = path, None
        else:
            url, params = f"{BASE}/{path}", dict(params, access_token=TOKEN)
        try:
            r = requests.get(url, params=params, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as ex:
            return False, {"message": type(ex).__name__}  # the text of a requests error quotes the URL
        if r.status_code != 200 or "error" in data:
            return False, data.get("error", data)
        return True, data


def clean(err):
    """insights.json is public: keep Meta's message, never anything that could hold the token."""
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    msg = msg.replace(TOKEN, "***") if TOKEN else msg
    return msg[:200]


def values(data):
    """Meta insights rows -> {metric: number}. Lifetime rows carry values[0].value; total_value wins if sent."""
    out = {}
    for row in data.get("data", []):
        vals = row.get("values") or [{}]
        out[row["name"]] = row.get("total_value", {}).get("value", vals[0].get("value"))
    return out


def list_reels(api):
    media, path = [], f"{USER}/media"
    params = {"fields": "id,caption,media_product_type,media_type,permalink,timestamp", "limit": 50}
    while path and len(media) < MAX_MEDIA:
        ok, data = api.get(path, **params)
        if not ok:
            raise SystemExit(f"could not list media: {clean(data)}")
        media += data.get("data", [])
        path, params = data.get("paging", {}).get("next"), {}
    return [m for m in media if m.get("media_product_type") == "REELS"]


def pick_metrics(api, reel_id):
    """Ask for every metric on the newest Reel; if Meta refuses the set, find the accepted ones one by one."""
    ok, _ = api.get(f"{reel_id}/insights", metric=",".join(METRICS))
    if ok:
        return list(METRICS), {}
    good, dropped = [], {}
    for m in METRICS:
        ok, err = api.get(f"{reel_id}/insights", metric=m)
        if ok:
            good.append(m)
        else:
            dropped[m] = clean(err)
    return good, dropped


def reel_numbers(api, reel_id, metrics):
    """{metric: value} for one Reel, plus the metrics it could not get. One refused metric must not blank
    the rest, so a refused set is asked again one metric at a time."""
    ok, data = api.get(f"{reel_id}/insights", metric=",".join(metrics))
    if ok:
        return values(data), []
    vals, missing = {}, []
    for m in metrics:
        ok, d = api.get(f"{reel_id}/insights", metric=m)
        if ok:
            vals.update(values(d))
        else:
            missing.append(m)
    return vals, missing


def account(api):
    ok, data = api.get("me", fields=",".join(ACCOUNT_FIELDS))
    if not ok:
        ok, data = api.get("me", fields="username,followers_count")
    if not ok:
        return {"error": clean(data)}
    return {k: data[k] for k in ACCOUNT_FIELDS if k in data}


def age_days(ts):
    """Days since an Instagram timestamp like 2026-10-19T23:03:12+0000 (UTC)."""
    try:
        return (time.time() - calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (TypeError, ValueError):
        return 0


def day_start(day):
    """Unix time when the Meta day `day` (YYYY-MM-DD, Pacific) starts: its Pacific midnight."""
    d = datetime.date.fromisoformat(day)
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=META_TZ).timestamp())


def day_params(day):
    """since/until that hold exactly one Meta day start (days are 23 to 25 hours apart)."""
    start = day_start(day)
    return {"period": "day", "metric_type": "total_value", "since": start - 43200, "until": start + 43200}


def days_to_read(have, now):
    """Complete Meta days to read this run: the newest REFRESH_DAYS always, then missing older days back to
    FIRST_DAY, newest first, at most BACKFILL_PER_RUN of them."""
    today = datetime.datetime.fromtimestamp(now, META_TZ).date()
    first = datetime.date.fromisoformat(FIRST_DAY)
    newest = [today - datetime.timedelta(days=i) for i in range(1, REFRESH_DAYS + 1)]
    out = [d.isoformat() for d in newest if d >= first]
    d, extra = today - datetime.timedelta(days=REFRESH_DAYS + 1), []
    while d >= first and len(extra) < BACKFILL_PER_RUN:
        if d.isoformat() not in have:
            extra.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return out, extra


def split(data):
    """{dimension value: number} from a total_value answer asked with one breakdown."""
    out = {}
    for row in data.get("data", []):
        for b in (row.get("total_value") or {}).get("breakdowns") or []:
            for r in b.get("results") or []:
                out[(r.get("dimension_values") or ["?"])[0]] = r.get("value")
    return out


def pick_account_metrics(api, day):
    """Ask for every account metric on one settled day; if Meta refuses the set, find the accepted ones."""
    ok, _ = api.get(f"{USER}/insights", metric=",".join(ACCOUNT_METRICS), **day_params(day))
    if ok:
        return list(ACCOUNT_METRICS), {}
    good, dropped = [], {}
    for m in ACCOUNT_METRICS:
        ok, err = api.get(f"{USER}/insights", metric=m, **day_params(day))
        if ok:
            good.append(m)
        else:
            dropped[m] = clean(err)
    return good, dropped


def account_day(api, day, metrics):
    """(numbers, error) for one Meta day. Reach is also split into followers / non-followers, and once Meta
    sends follows_and_unfollows (100+ followers) it is split into follows (FOLLOWER) and unfollows
    (NON_FOLLOWER), as Meta files them. A metric Meta leaves out of the answer is simply absent."""
    ok, data = api.get(f"{USER}/insights", metric=",".join(metrics), **day_params(day))
    if not ok:
        return None, clean(data)
    vals = {k: v for k, v in values(data).items() if v is not None}
    if "reach" in vals:
        ok, d = api.get(f"{USER}/insights", metric="reach", breakdown="follow_type", **day_params(day))
        parts = split(d) if ok else {}
        if "FOLLOWER" in parts or "NON_FOLLOWER" in parts:
            vals["reach_followers"] = parts.get("FOLLOWER", 0)
            vals["reach_non_followers"] = parts.get("NON_FOLLOWER", 0)
    if vals.get("follows_and_unfollows") is not None:
        ok, d = api.get(f"{USER}/insights", metric="follows_and_unfollows", breakdown="follow_type",
                        **day_params(day))
        parts = split(d) if ok else {}
        if parts:
            vals["follows"] = parts.get("FOLLOWER", 0)
            vals["unfollows"] = parts.get("NON_FOLLOWER", 0)
    return vals, None


def follower_series(api, days):
    """Meta's follower_count time series over `days` ({day: number}); empty under 100 followers."""
    if not days:
        return {}
    ok, data = api.get(f"{USER}/insights", metric="follower_count", period="day",
                       since=day_start(min(days)) - 43200, until=day_start(max(days)) + 43200)
    out = {}
    for row in data.get("data", []) if ok else []:
        for v in row.get("values") or []:
            try:
                mark = datetime.datetime.strptime(v["end_time"][:19], "%Y-%m-%dT%H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            # end_time is the midnight the day starts at (see META_TZ above)
            out[mark.replace(tzinfo=datetime.timezone.utc).astimezone(META_TZ).date().isoformat()] = v.get("value")
    return out


def account_daily(api, old, now):
    """The account_daily block: old days kept as they were, the days read this run updated in place."""
    prev = old.get("account_daily") or {}
    days = {d: dict(v) for d, v in (prev.get("days") or {}).items()}
    if days and prev.get("basis_version", 1) < 2:  # the first run filed every day one day early: move each once
        days = {(datetime.date.fromisoformat(d) + datetime.timedelta(days=1)).isoformat(): v for d, v in days.items()}
    newest, extra = days_to_read(days, now)
    if not newest:
        return dict(prev, days=days, basis_version=BASIS)
    probe = newest[min(2, len(newest) - 1)]  # a day Meta has settled (about 3 days back)
    metrics, dropped = pick_account_metrics(api, probe)
    errors, stamp = {}, time.strftime("%Y-%m-%d", time.gmtime(now))
    if metrics:
        for i, day in enumerate(newest + extra):
            vals, err = account_day(api, day, metrics)
            if err or not vals:  # a quiet day still comes back as zeros; nothing at all means no data
                errors[day] = err or "no numbers for this day"
                if i >= len(newest):
                    break  # past Meta's history, or a bad moment: the backfill tries again next run
                continue
            row = days.setdefault(day, {})
            row.update(vals)
            row["read"] = stamp
        for day, n in follower_series(api, newest).items():
            if day in days and n is not None:
                days[day]["follower_count"] = n
    return {"basis": "one row per Meta day, midnight to midnight US Pacific; read = UTC date last read",
            "basis_version": BASIS, "metrics": metrics or prev.get("metrics", []), "dropped": dropped,
            "errors": errors, "days": dict(sorted(days.items()))}


def build(api, old, now=None):
    now = time.time() if now is None else now
    reels = list_reels(api)
    # test the metric set on a Reel a few days old: a Reel posted minutes ago may have no numbers yet
    probe = next((m for m in reels if age_days(m.get("timestamp")) >= 2), reels[0] if reels else None)
    metrics, dropped = pick_metrics(api, probe["id"]) if probe else ([], {})
    rows = []
    for m in reels:
        vals, missing = reel_numbers(api, m["id"], metrics) if metrics else ({}, [])
        row = {"id": m["id"], "permalink": m.get("permalink", ""), "timestamp": m.get("timestamp", ""),
               "caption": (m.get("caption") or "").split("\n")[0][:80], **{k: vals.get(k) for k in metrics}}
        if missing:
            row["missing"] = missing
        rows.append(row)
    acct = account(api)
    history = dict(old.get("followers_history", {}))
    if isinstance(acct.get("followers_count"), int):
        history[time.strftime("%Y-%m-%d", time.gmtime(now))] = acct["followers_count"]
    try:  # the daily block must never cost the Reel numbers: on a surprise it keeps the old days as they were
        daily = account_daily(api, old, now)
    except Exception as ex:  # noqa: BLE001
        daily = dict(old.get("account_daily") or {}, errors={"run": f"{type(ex).__name__}: {clean(str(ex))}"})
    return {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)), "updated_epoch": int(now),
            "api": BASE.rsplit("/", 1)[1], "account": acct, "followers_history": history,
            "metrics": metrics, "dropped": dropped,
            "units": {"ig_reels_avg_watch_time": "ms", "ig_reels_video_view_total_time": "ms"},
            "calls": api.calls, "reels": rows, "account_daily": daily}


def main():
    if not TOKEN or not USER:
        print("the IG_ACCESS_TOKEN / IG_USER_ID secrets are not set; nothing read")
        return 1
    try:
        old = json.load(open(OUT, encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    out = build(Api(), old)
    if not out["metrics"] and old.get("metrics"):  # a bad day at Meta must not blank yesterday's numbers
        print(f"every metric refused today, insights.json left as it was: {json.dumps(out['dropped'])[:500]}")
        return 1
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    acct = out["account"]
    print(f"{len(out['reels'])} Reels, {out['calls']} calls, followers {acct.get('followers_count', '?')}")
    print("metrics: " + (", ".join(out["metrics"]) or "none"))
    for k, v in out["dropped"].items():
        print(f"dropped {k}: {v}")
    if acct.get("error"):
        print(f"account fields refused: {acct['error']}")
    daily = out.get("account_daily") or {}
    days = daily.get("days") or {}
    print(f"account days: {len(days)} kept ({min(days, default='-')} to {max(days, default='-')}); "
          f"metrics: {', '.join(daily.get('metrics') or []) or 'none'}")
    for k, v in (daily.get("dropped") or {}).items():
        print(f"account dropped {k}: {v}")
    for k, v in list((daily.get("errors") or {}).items())[:5]:
        print(f"account day {k} not read: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only Reel insights for the Instagram account, run once a day by .github/workflows/insights.yml.

Reads every posted Reel's lifetime numbers plus the account's follower count and writes insights.json.
It only READS from Instagram: no posting, no token refresh, and it never opens queue.json. The workflow
commits insights.json alone, so it cannot disturb the poster (post.yml / poster.py commits queue.json).
In the app repo, `ig_publish.py insights --cloud` turns insights.json into marketing/social/insights.csv.

Metrics are the Reels metrics Meta lists for the Instagram API with Instagram Login. A metric the API
refuses is left out for that run (named under "dropped"), never fatal. Watch times are milliseconds,
as Meta sends them. Meta's numbers can lag up to 48 hours and count organic activity only.
Doc: https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
"""
import calendar, json, os, sys, time
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


def build(api, old):
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
        history[time.strftime("%Y-%m-%d", time.gmtime())] = acct["followers_count"]
    return {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "updated_epoch": int(time.time()),
            "api": BASE.rsplit("/", 1)[1], "account": acct, "followers_history": history,
            "metrics": metrics, "dropped": dropped,
            "units": {"ig_reels_avg_watch_time": "ms", "ig_reels_video_view_total_time": "ms"},
            "calls": api.calls, "reels": rows}


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
    return 0


if __name__ == "__main__":
    sys.exit(main())

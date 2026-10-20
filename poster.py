"""Post whatever is due in queue.json to Instagram. Run by .github/workflows/post.yml
every 15 minutes, so nothing depends on a home PC being on.

queue.json is written by scripts/ig_publish.py in the app repo (it uploads the media to
this repo's release first, so every entry already carries public URLs). This script only
reads the queue, posts, and writes the result back. The workflow commits queue.json.

Entry: {id, at_epoch, caption, status, video_url | image_urls, cover_url, thumb_ms, share_to_feed, trial}
Status: pending -> posting -> done | failed | stale. An entry left at "posting" means a
run died mid-post: it is never retried by itself, so nothing can post twice.

Trial Reels (added 2026-10-20): an entry with "trial": true is posted as a trial Reel, shown to
non-followers first. SS_PERFORMANCE means Instagram moves it to followers by itself if it does well
(MANUAL would wait for a tap in the app). Entries without the flag send exactly what they did before.
Doc: https://developers.facebook.com/docs/instagram-platform/content-publishing/ ("Trial Reels posts").
If Meta refuses the trial, the Reel goes out as a normal one and the entry gets "trial_refused".
"""
import json, os, subprocess, sys, time
import requests

BASE = "https://graph.instagram.com/v23.0"
MAX_LATE_H = 24
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER = os.environ.get("IG_USER_ID", "")
TRIAL_PARAMS = json.dumps({"graduation_strategy": "SS_PERFORMANCE"}, separators=(",", ":"))


def clean(text):
    """queue.json is public: an error text (a requests error quotes the URL) must never carry the token."""
    text = str(text)
    return text.replace(TOKEN, "***") if TOKEN else text


def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{BASE}/{path}", params=params, timeout=120)
    data = r.json()
    if r.status_code != 200 or "error" in data:
        raise RuntimeError(f"API error on {path}: {json.dumps(data.get('error', data))[:500]}")
    return data


def wait(container, what, tries=90):
    for _ in range(tries):
        st = api("GET", container, fields="status_code,status")
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{what} processing failed: {json.dumps(st)}")
        time.sleep(10)
    raise RuntimeError(f"timed out waiting for {what}")


def reel_container(e, p):
    """Create the Reel container and return its id. Neither the custom cover nor the trial may cost the
    post: when Meta refuses a request, try again without the cover, then (trial entries) as a normal Reel.
    Without "trial" this is the same one or two calls as before the trial option existed."""
    tries = [dict(cover_url=e["cover_url"], **p)] if e.get("cover_url") else []
    tries.append(p)
    if e.get("trial"):
        tries = [dict(t, trial_params=TRIAL_PARAMS) for t in tries] + tries
    for i, t in enumerate(tries):
        try:
            return api("POST", f"{USER}/media", **t)["id"]
        except RuntimeError as ex:
            if i == len(tries) - 1:
                raise
            if "trial_params" in t and "trial_params" not in tries[i + 1]:
                e["trial_refused"] = clean(ex)[:300]
                print(f"trial refused ({clean(ex)[:160]}), posting as a normal Reel")
            else:
                print(f"cover refused ({clean(ex)[:160]}), posting without it")


def publish(e):
    if e.get("image_urls"):
        kids = []
        for u in e["image_urls"]:
            c = api("POST", f"{USER}/media", image_url=u, is_carousel_item="true")
            wait(c["id"], "image")
            kids.append(c["id"])
        cont = api("POST", f"{USER}/media", media_type="CAROUSEL", children=",".join(kids), caption=e["caption"])["id"]
    else:
        p = {"media_type": "REELS", "video_url": e["video_url"], "caption": e["caption"],
             "share_to_feed": "true" if e.get("share_to_feed", True) else "false"}
        if e.get("thumb_ms"):
            p["thumb_offset"] = str(e["thumb_ms"])
        cont = reel_container(e, p)
    wait(cont, "post")
    media = api("POST", f"{USER}/media_publish", creation_id=cont)["id"]
    return api("GET", media, fields="permalink").get("permalink", "?")


def save(q):
    with open("queue.json", "w", encoding="utf-8") as f:
        json.dump(q, f, indent=1, ensure_ascii=False)
        f.write("\n")


def push(msg):
    """Commit queue.json right away so a status is on record before and after each post."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    subprocess.run(["git", "add", "queue.json"], check=True)  # token_refreshed.txt is added where it is written
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"], check=True)
    raise RuntimeError("could not push queue.json")


def keep_token_alive():
    """A long-lived Instagram token dies after 60 days unless it is refreshed. Refresh it
    weekly. The secret cannot be rewritten from here, so if Instagram ever hands back a
    DIFFERENT token the run fails loudly: the owner then updates the IG_ACCESS_TOKEN secret."""
    try:
        last = int(open("token_refreshed.txt").read().strip())
    except (OSError, ValueError):
        last = 0
    if time.time() - last < 7 * 86400:
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": TOKEN}, timeout=60).json()
    if "access_token" not in r:
        print(f"token refresh did not work: {json.dumps(r)[:300]}")
        return
    with open("token_refreshed.txt", "w") as f:
        f.write(str(int(time.time())))
    if os.environ.get("GITHUB_ACTIONS"):
        subprocess.run(["git", "add", "token_refreshed.txt"], check=True)
    print(f"token refreshed, good for about {r.get('expires_in', 0) // 86400} days")
    if r["access_token"] != TOKEN:
        # Keep posting. The current token works until its own 60 days run out; stopping here (before
        # token_refreshed.txt was committed) made every later run refresh again and never post (2026-10-16).
        with open("token_new_since.txt", "w") as f:
            f.write(time.strftime("%Y-%m-%d") + "\n")
        if os.environ.get("GITHUB_ACTIONS"):
            subprocess.run(["git", "add", "token_new_since.txt"], check=True)
        print("WARNING: Instagram issued a NEW token on refresh. The IG_ACCESS_TOKEN secret still works for now; "
              "update it before 60 days pass (date in token_new_since.txt).")


def main():
    q = json.load(open("queue.json", encoding="utf-8"))
    if TOKEN:
        keep_token_alive()
        push("token refreshed")
    now = time.time()
    due = sorted((e for e in q if e["status"] == "pending" and e["at_epoch"] <= now), key=lambda e: e["at_epoch"])
    if not due:
        print("nothing due")
        return
    if not TOKEN or not USER:
        print(f"{len(due)} due but the IG_ACCESS_TOKEN / IG_USER_ID secrets are not set; holding")
        return
    for e in due:
        late_h = (time.time() - e["at_epoch"]) / 3600
        if late_h > MAX_LATE_H:
            e["status"], e["error"] = "stale", f"{late_h:.0f} h late, not posted"
            save(q); push(f"{e['id']} stale")
            continue
        e["status"] = "posting"
        save(q); push(f"{e['id']} posting")
        try:
            e["result"] = publish(e)
            e["status"] = "done"
            e["posted_epoch"] = int(time.time())
            print(f"{e['id']} DONE {e['result']}")
        except Exception as ex:
            e["status"], e["error"] = "failed", clean(ex)[:300]
            print(f"{e['id']} FAILED {e['error']}")
        save(q); push(f"{e['id']} {e['status']}")


if __name__ == "__main__":
    sys.exit(main())

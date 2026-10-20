# Seed Oil Tracker media

Public hosting for Seed Oil Tracker videos. Instagram fetches Reels from the release assets here; the app itself lives elsewhere.

queue.json is the posting schedule. A scheduled workflow (poster.py) posts each entry at its time, so nothing depends on a home computer being on.

An entry with `"trial": true` posts as a trial Reel (shown to non-followers first; Instagram moves it to followers if it performs).

insights.json holds each posted Reel's numbers, the follower count, and the account's numbers per day (`account_daily`: profile views, bio-link taps, reach from followers and non-followers; one row per Meta day, midnight to midnight US Pacific, old days never dropped). A separate read-only daily workflow (insights.py) writes it; it never touches queue.json. Tests: `python -m unittest discover -s tests`.

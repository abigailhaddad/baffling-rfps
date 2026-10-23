"""Rate one day's new SAM.gov postings: data/days/<day>.json.

For each day after the newest file in data/days through yesterday (US
Eastern), in order:
  1. new postings first posted that day (sam.new_postings)
  2. prescreen (cheap model) on all of them, pass 1 on those rated 2+ (measured 2026-10-19..21: 1+ would add 389 reads a day for 1 of 21 final flags)
  3. attachments for those pass 1 rated 2+, then pass 2 and the verifier
     (all four stages as Message Batches, under a daily cost cap: batch.py)
  4. write data/days/<day>.json; add that day's solicitations to data/seen.csv

The extract is downloaded once per run. A day's file is never overwritten.
data/seen.csv must be seeded first (--seed-before), or the run refuses.

Usage:
  uv run python -m baffling.daily                         # daily
  uv run python -m baffling.daily --day 2026-10-21        # one day
  uv run python -m baffling.daily --seed-before 2026-10-19  # once, at the start
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import attachments, batch, rate, sam

ROOT = Path(__file__).resolve().parent.parent
DAYS = ROOT / "data" / "days"
SEEN = ROOT / "data" / "seen.csv"
WORK = ROOT / "work"
MAX_CATCH_UP = 7
KEEP = ["key", "notice_id", "first_notice_id", "sol", "type", "title", "agency", "subtier", "office", "naics", "psc",
        "set_aside", "first_posted", "versions", "deadline", "window_days", "place", "link"]

log = logging.getLogger(__name__)


def days_to_run(today_et: dt.date) -> list[dt.date]:
    have = sorted(p.stem for p in DAYS.glob("*.json"))
    yesterday = today_et - dt.timedelta(days=1)
    if not have:
        return [yesterday]
    d = dt.date.fromisoformat(have[-1]) + dt.timedelta(days=1)
    out = []
    while d <= yesterday:
        out.append(d)
        d += dt.timedelta(days=1)
    return out[-MAX_CATCH_UP:]


def _cached(path: Path, fn):
    """Run fn once per day and stage: a crash or rerun never pays for a stage twice."""
    if path.exists():
        return {k: tuple(v) for k, v in json.loads(path.read_text()).items()}
    out = fn()
    path.write_text(json.dumps(out))
    return out


def run_day(day: dt.date, gs: dict, seen: dict, extract_info: dict, c=None) -> dict:
    new, skipped = sam.new_postings(gs, day, seen)
    log.info("%s: %d new postings", day, len(new))
    c = c or rate.client()
    budget = batch.Budget()
    cache = WORK / "cache" / day.isoformat()
    cache.mkdir(parents=True, exist_ok=True)

    # prescreen: every posting, cheap model. A posting whose prescreen failed goes on (never dropped silently).
    reqs = rate.pass1_requests(new, rate.PRESCREEN_MODEL, "pre")
    pre = rate.pass1_collect(_cached(cache / "prescreen.json", lambda: batch.run(
        c, reqs, "prescreen", budget, valid=lambda cid, p: rate.pass1_valid(cid, p, len(new)))), len(new))
    on = [i for i, o in enumerate(pre) if o is None or o["severity"] >= 2]

    # pass 1 on what the prescreen passed
    sub = [new[i] for i in on]
    reqs = rate.pass1_requests(sub, rate.FIRST_MODEL, "p1")
    keep, cut = budget.fit(list(reqs.items()), "pass1")
    if cut:
        raise SystemExit(f"{day}: pass 1 alone would exceed the ${budget.cap} daily cap; not running the day")
    r1 = rate.pass1_collect(_cached(cache / "pass1.json", lambda: batch.run(
        c, reqs, "pass1", budget, valid=lambda cid, p: rate.pass1_valid(cid, p, len(sub)))), len(sub))
    p1: dict[int, dict | None] = {i: r1[j] for j, i in enumerate(on)}
    cands = sorted((i for i in on if p1[i] and p1[i]["severity"] >= 2), key=lambda i: -p1[i]["severity"])
    log.info("%s: prescreen passed %d, pass 1 flagged %d; reading their attachments", day, len(on), len(cands))

    session = requests.Session()
    with ThreadPoolExecutor(4) as pool:
        atts = dict(zip(cands, pool.map(lambda i: attachments.fetch(new[i]["notice_id"], WORK / "att", session), cands)))

    # pass 2, most suspicious first; whatever does not fit the cap is recorded as not rated
    reqs = {new[i]["notice_id"]: rate.pass2_request(new[i], atts[i]) for i in cands}
    keep, cut = budget.fit(list(reqs.items()), "pass2")
    raw = _cached(cache / "pass2.json", lambda: batch.run(c, dict(keep), "pass2", budget))
    p2 = {}
    for i in cands:
        nid = new[i]["notice_id"]
        p2[i] = rate.pass2_apply(new[i], atts[i], *raw[nid]) if nid in raw else {"not_rated": "daily budget", "tokens": [0, 0]}

    # verify what is still rated 2 or 3; fail closed if it cannot be checked
    vs = [i for i in cands if (p2[i].get("severity") or 0) >= 2]
    reqs = {new[i]["notice_id"]: rate.verify_request(new[i], atts[i].get("files", []), atts[i].get("error"), p2[i])
            for i in vs}
    keep, cut = budget.fit(list(reqs.items()), "verify")
    raw = _cached(cache / "verify.json", lambda: batch.run(c, dict(keep), "verify", budget))
    for i in vs:
        nid = new[i]["notice_id"]
        rate.verify_apply(p2[i], *(raw[nid] if nid in raw else (None, "daily budget", [0, 0])))

    postings = []
    for i, p in enumerate(new):
        row = {k: p[k] for k in KEEP}
        row["pass1"] = {"prescreen": pre[i]["severity"] if pre[i] else None,
                        "severity": p1[i]["severity"] if p1.get(i) else None,
                        "summary": (p1.get(i) or pre[i] or {}).get("summary", ""),
                        "failed": pre[i] is None or (i in p1 and p1[i] is None)}
        if i in p2:
            row["description"] = p["description"]
            row["pass2"] = p2[i]
        postings.append(row)

    def sev(o):
        return (o.get("pass2") or {}).get("severity")

    return {
        "day": day.isoformat(), "extract": extract_info, "skipped": skipped,
        "models": {"prescreen": rate.PRESCREEN_MODEL, "pass1": rate.FIRST_MODEL, "pass2": rate.SECOND_MODEL,
                   "verify": rate.VERIFY_MODEL},
        "counts": {"new": len(new), "prescreen_passed": len(on), "pass1_flagged": len(cands),
                   "pass1_failed": sum(o["pass1"]["failed"] for o in postings),
                   "pass2": {str(s): sum(sev(o) == s for o in postings) for s in range(4)},
                   "not_rated": sum(1 for o in postings if (o.get("pass2") or {}).get("not_rated")),
                   "verify_failed": sum(1 for o in postings if (o.get("pass2") or {}).get("verify_failed")),
                   "budget_cut": sum(1 for o in postings if "daily budget" in
                                     ((o.get("pass2") or {}).get("not_rated") or (o.get("pass2") or {}).get("verify_failed") or ""))},
        "cost_usd": {"total": round(budget.spent, 4), **budget.by_stage, "cap": budget.cap},
        "postings": postings,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="baffling.daily")
    ap.add_argument("--day", help="run only this day (YYYY-MM-DD)")
    ap.add_argument("--extract", help="use this extract instead of downloading it")
    ap.add_argument("--seed-before", help="add every solicitation first posted before this day to seen, then stop")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = Path(a.extract) if a.extract else sam.download(WORK / "extract.csv")
    info = {"file": path.name, "downloaded_utc": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            .replace(microsecond=0).isoformat()}
    gs = sam.groups(sam.read_rows(path))
    seen = sam.load_seen(SEEN)
    if a.seed_before:
        n = sam.mark_seen(seen, gs, before=dt.date.fromisoformat(a.seed_before))
        sam.save_seen(seen, SEEN)
        print(json.dumps({"seeded": n, "seen": len(seen)}))
        return
    if not seen:
        raise SystemExit("data/seen.csv is empty: seed it first with --seed-before")
    today = dt.datetime.now(sam.ET).date()
    days = [dt.date.fromisoformat(a.day)] if a.day else days_to_run(today)
    DAYS.mkdir(parents=True, exist_ok=True)
    for day in days:
        out = DAYS / f"{day}.json"
        if out.exists():
            raise SystemExit(f"{out} exists; not overwriting")
        result = run_day(day, gs, seen, info)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1, ensure_ascii=False))
        tmp.replace(out)
        # only solicitations first posted on or before this day: later ones are the next days' new postings
        sam.mark_seen(seen, gs, before=day + dt.timedelta(days=1))
        sam.save_seen(seen, SEEN)
        print(json.dumps({"day": result["day"], **result["counts"], "cost_usd": result["cost_usd"]}))


if __name__ == "__main__":
    main()

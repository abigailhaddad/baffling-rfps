"""Build the week's issue: data/issues/<date>.json.

Covers every day in data/days after the previous issue's last day. One issue
per Monday-Sunday week: refuses if the newest issue is in the same week.

The winner:
  1. finalists = postings rated 3 after pass 2 + verifier (2 if there are none),
     with at least one surviving issue
  2. every listed posting (not just the finalists) is re-checked live on SAM.gov: dropped if cancelled, archived
     or deleted; if amended, its quotes are re-checked against the newest
     version's text and attachments, and issues whose quotes are gone dropped
  3. Claude picks one and writes the page headline and dek, choosing which of
     its already-verified issues to show; it cannot add quotes or claims
  4. the headline and dek go through the same independent verifier, and every
     number in them must appear in the facts or quotes; if they fail twice, no
     issue is built (the run fails loudly)

Everything else on the page is computed or is an already-verified issue.

Usage: uv run python -m baffling.issue [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
from pathlib import Path

import requests

from . import attachments, rate, sam
from .daily import DAYS, ROOT, WORK

ISSUES = ROOT / "data" / "issues"
MAX_FINALISTS = 25  # a typical week has ~20 severity-2 postings; the pick should see them all
PICK_MODEL = rate.VERIFY_MODEL  # once a week, a few cents: the strongest model writes the page
OPP_URL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"

log = logging.getLogger(__name__)

PICK = """Below are this week's most baffling new U.S. federal contracting postings from SAM.gov. Each has issues that have already been checked against its documents.

Pick the ONE posting a general reader would find most baffling: the clearest, most concrete, most self-evidently wrong. Prefer problems anyone can see from the quotes (a work statement for the wrong service, a deadline before the posting date, a drafting note left in) over technical ones.

Then write its page:
- headline: under 12 words, plain, factual, striking; no exaggeration, no sarcasm, no claims the issues do not support
- dek: one or two sentences saying what the posting is and what is wrong with it, using only the issues' content
- hero: the issue whose quote shows the problem best on its own
- support: up to 2 more issues worth showing with their quotes
- also: up to 4 more issues for a short list
Refer to issues by their id. Use only the facts and issues given.

{finalists}"""

PICK_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["posting", "headline", "dek", "hero", "support", "also"], "properties": {
                   "posting": {"type": "integer"}, "headline": {"type": "string"}, "dek": {"type": "string"},
                   "hero": {"type": "string"}, "support": {"type": "array", "items": {"type": "string"}},
                   "also": {"type": "array", "items": {"type": "string"}}}}


def same_week(a: dt.date, b: dt.date) -> bool:
    return a.isocalendar()[:2] == b.isocalendar()[:2]


def load_issues() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(ISSUES.glob("*.json"))]


def days_after(last: str | None) -> list[dict]:
    out = []
    for p in sorted(DAYS.glob("*.json")):
        if last is None or p.stem > last:
            out.append(json.loads(p.read_text()))
    return out


def rated(days: list[dict]) -> list[dict]:
    return [p for d in days for p in d["postings"] if (p.get("pass2") or {}).get("severity") is not None]


def listed(days: list[dict]) -> list[dict]:
    """Every posting rated 2 or 3 with issues left, most severe first."""
    return sorted((p for p in rated(days) if p["pass2"]["severity"] >= 2 and p["pass2"]["issues"]),
                  key=lambda p: (-p["pass2"]["severity"], p["first_posted"]))


def finalists(ps: list[dict]) -> list[dict]:
    """From live-checked postings: those rated 3, or 2 if there are none."""
    ps = [p for p in ps if p["pass2"]["issues"]]  # the pick writes and verifies its own headline
    for level in (3, 2):
        f = [p for p in ps if p["pass2"]["severity"] == level]
        if f:
            f.sort(key=lambda p: (-len(p["pass2"]["issues"]), p["first_posted"]))
            return f[:MAX_FINALISTS]
    return []


def live_check(p: dict, session: requests.Session) -> dict | None:
    """Re-check a finalist on SAM.gov now. Returns the posting with surviving
    issues and a `live` note, or None if it is gone or nothing survives."""
    r = session.get(OPP_URL.format(notice_id=p["notice_id"]), timeout=60, headers={"User-Agent": sam.USER_AGENT})
    r.raise_for_status()
    o = r.json()
    if o.get("cancelled") or o.get("archived") or o.get("deleted"):
        log.info("finalist %s dropped: cancelled/archived/deleted", p["sol"])
        return None
    live = {"checked_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "amended": False}
    notice_id = p["notice_id"]
    if not o.get("latest", True):
        href = ((o.get("_links") or {}).get("opportunity:publish:latest") or {}).get("href", "")
        notice_id = href.rstrip("/").split("/")[-1] or notice_id
        r = session.get(OPP_URL.format(notice_id=notice_id), timeout=60, headers={"User-Agent": sam.USER_AGENT})
        r.raise_for_status()
        o = r.json()
        live.update(amended=True, latest_notice_id=notice_id)
    body = " ".join(d.get("body", "") for d in (o.get("description") or []))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    files = attachments.fetch(notice_id, WORK / "att_live", session)
    if files.get("error"):
        log.info("finalist %s dropped: %s", p["sol"], files["error"])
        return None
    now_posting = {**p, "description": text or p.get("description", "")}
    keep = []
    for x in p["pass2"]["issues"]:
        ok = all(rate.locate(e["quote"], e["source"], now_posting, files["files"]) for e in x["evidence"])
        (keep.append(x) if ok else log.info("finalist %s: issue no longer in the live documents", p["sol"]))
    if not keep:
        return None
    return {**p, "pass2": {**p["pass2"], "issues": keep}, "live": live, "_files": files, "_text": now_posting}


def numbers_ok(text: str, p: dict, issues: list[dict]) -> bool:
    """Every number in the headline/dek must appear in the facts or quotes."""
    return rate.numbers_supported(text, rate.haystack(p, [], [e for x in issues for e in x["evidence"]]))


def pick(c, fs: list[dict]) -> dict:
    blocks = []
    for i, p in enumerate(fs):
        lines = [f"[posting {i}] {rate.facts(p)}"]
        for j, x in enumerate(p["pass2"]["issues"]):
            q = " | ".join(f'{e["source"]}: "{e["quote"]}"' for e in x["evidence"]) or "(no quote: computed window)"
            lines.append(f"  issue {i}.{j} ({x['kind']}): {x['why']}\n     quotes: {q}")
        blocks.append("\n".join(lines))
    last = None
    for attempt in range(2):
        out, stop, tok = rate.call(c, rate.params(PICK_MODEL, PICK.format(finalists="\n\n".join(blocks)), PICK_SCHEMA, 3000))
        if out is None or not 0 <= out["posting"] < len(fs):
            last = f"pick failed ({stop})"
            continue
        p = fs[out["posting"]]
        ids = {f"{out['posting']}.{j}": x for j, x in enumerate(p["pass2"]["issues"])}
        chosen = [out["hero"]] + out["support"][:2] + out["also"][:4]
        if any(i not in ids for i in chosen) or len(set(chosen)) != len(chosen):
            last = "pick named an issue that does not exist"
            continue
        if not (numbers_ok(out["headline"], p, p["pass2"]["issues"]) and numbers_ok(out["dek"], p, p["pass2"]["issues"])):
            last = "headline or dek has a number not in the facts or quotes"
            continue
        check = {"headline": out["headline"] + " " + out["dek"], "issues": [], "dropped_issues": [], "severity": 3}
        rate.verify_apply(check, *rate.call(c, rate.verify_request(p["_text"], p["_files"]["files"], None, check)))
        if check.get("verify_failed") or not check["headline"]:
            last = "verifier rejected the headline/dek: " + check.get("dropped_headline", {}).get("reason", "")
            continue
        return {"posting": p, "headline": out["headline"], "dek": out["dek"],
                "hero": ids[out["hero"]], "support": [ids[i] for i in out["support"][:2]],
                "also": [ids[i] for i in out["also"][:4]], "tokens": tok}
    raise SystemExit(f"no winner: {last}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="baffling.issue")
    ap.add_argument("--date", help="issue date (default: today, US Eastern)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    date = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(sam.ET).date()
    issues = load_issues()
    if issues and same_week(dt.date.fromisoformat(issues[-1]["date"]), date):
        print(json.dumps({"skipped": f"an issue was already built this week ({issues[-1]['date']})"}))
        return
    days = days_after(issues[-1]["window"]["to"] if issues else None)
    if not days:
        raise SystemExit("no new days to cover")
    session = requests.Session()
    # the whole list is re-checked live, not just the finalists: an amended posting must not
    # be shown with problems its amendment fixed
    before = listed(days)
    shown = [x for x in (live_check(p, session) for p in before) if x]
    checked = finalists(shown)
    if not checked:
        raise SystemExit("no finalist survived the live check")
    c = rate.client()
    w = pick(c, checked)
    p = w["posting"]
    strip = lambda x: {k: v for k, v in x.items() if not k.startswith("_") and k != "description"}
    issue = {
        "date": date.isoformat(),
        "window": {"from": days[0]["day"], "to": days[-1]["day"]},
        "built_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "counts": {"new": sum(d["counts"]["new"] for d in days),
                   "pass1_flagged": sum(d["counts"]["pass1_flagged"] for d in days),
                   "severity3": sum(1 for x in shown if x["pass2"]["severity"] == 3),
                   "severity2": sum(1 for x in shown if x["pass2"]["severity"] == 2),
                   "dropped_by_live_check": len(before) - len(shown),
                   "not_rated": sum(d["counts"]["not_rated"] for d in days),
                   "verify_failed": sum(d["counts"]["verify_failed"] for d in days),
                   "finalists": len(checked)},
        "winner": {**strip(p), "headline": w["headline"], "dek": w["dek"], "hero": w["hero"],
                   "support": w["support"], "also": w["also"]},
        "list": [{**strip(x), "pass2": {k: x["pass2"][k] for k in ("severity", "headline", "issues")}} for x in shown],
        "model": PICK_MODEL,
    }
    ISSUES.mkdir(parents=True, exist_ok=True)
    out = ISSUES / f"{date}.json"
    out.write_text(json.dumps(issue, indent=1, ensure_ascii=False))
    print(json.dumps({"issue": out.name, "winner": p["sol"], "headline": w["headline"], **issue["counts"]}))


if __name__ == "__main__":
    main()

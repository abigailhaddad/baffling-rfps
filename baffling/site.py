"""Build the static site (site/dist) from data/issues/*.json.

    index.html              the newest week's Baffling RFP
    weeks/<date>.html       each week's Baffling RFP
    weeks/<date>-all.html   everything flagged that week
    past.html, about.html, feed.xml, style.css, 404.html

No JavaScript, no cookies. Every value is HTML-escaped; links are kept only
if they are plain http(s) URLs.

Usage: uv run python -m baffling.site --out site/dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
from email.utils import format_datetime
from pathlib import Path

from .issue import ISSUES

TITLE = "Baffling RFP of the Week"
# baffling-rfps.pages.dev until the baffling.abigailhaddad.com CNAME exists
# (2026-10-23: launched on pages.dev first); switch back once it resolves.
SITE_URL = "https://baffling-rfps.pages.dev"
PROJECT_URL = "https://github.com/abigailhaddad/baffling-rfps"
ISSUE_URL = PROJECT_URL + "/issues/new"
TAGLINE = ("The most baffling new federal contracting posting on SAM.gov each week, plus every other one "
           "that looked wrong, found by Claude, with every quote checked against the documents")
KIND_LABEL = {"unclear": "Unclear", "contradictory": "Contradicts itself", "wrong_content": "Wrong or leftover text",
              "impossible": "Impossible as written", "rushed": "Rushed"}
FONTS = ("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,800&family=IBM+Plex+Mono:wght@400;600"
         "&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap")

CSS = """:root{--bg:#f6f1e7;--ink:#1c1a16;--muted:#6b6457;--rule:#d9cfbb;--red:#c4271b;--hl:#ffe27a;--card:#fffdf7}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#1a1916;--ink:#eee8da;--muted:#a8a08f;--rule:#3d3a32;--red:#ff6253;--hl:#6b5a12;--card:#22211d}}
:root[data-theme="dark"]{--bg:#1a1916;--ink:#eee8da;--muted:#a8a08f;--rule:#3d3a32;--red:#ff6253;--hl:#6b5a12;--card:#22211d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.6 "Source Serif 4",Georgia,serif}
a{color:inherit}
.top{max-width:680px;margin:0 auto;padding:18px 16px 0;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font:600 12px/1.4 "IBM Plex Mono",monospace;letter-spacing:.12em;text-transform:uppercase}
.top a{text-decoration:none}.top nav a{margin-left:14px;color:var(--muted)}.top nav a:first-child{margin-left:0}
main{max-width:680px;margin:0 auto;padding:28px 16px 40px;overflow-wrap:anywhere}
.label{font:600 12px/1.4 "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.stamp{display:inline-block;margin:26px 0 18px;padding:6px 12px 4px;border:3px solid var(--red);border-radius:6px;color:var(--red);font:800 22px/1 Fraunces,serif;letter-spacing:.12em;transform:rotate(-4deg)}
h1{font:800 clamp(34px,7vw,56px)/1.03 Fraunces,Georgia,serif;letter-spacing:-.015em;margin:0 0 18px}
.dek{font-size:20px;margin:0 0 14px}
.meta{font:400 13px/1.6 "IBM Plex Mono",monospace;color:var(--muted);margin:0 0 40px}
figure{margin:0 0 38px}
blockquote{margin:0;font:600 17px/1.5 "IBM Plex Mono",monospace;border-left:3px solid var(--red);padding-left:16px}
blockquote+blockquote{margin-top:14px}
figure.hero blockquote{font-size:clamp(19px,3.4vw,24px);line-height:1.38;border:0;border-top:2px solid var(--ink);border-bottom:2px solid var(--ink);padding:20px 0}
figure.hero blockquote+blockquote{border-top:0;margin-top:0}
.fact{font:600 clamp(19px,3.4vw,24px)/1.38 "IBM Plex Mono",monospace}
figcaption{margin-top:10px;font-size:16px;color:var(--muted)}
figcaption strong{color:var(--ink);font-weight:600}
.src{font:400 12px/1.4 "IBM Plex Mono",monospace;color:var(--muted);display:block;margin-top:4px}
h2{font:800 22px/1.2 Fraunces,serif;margin:44px 0 12px}
ul.also{padding-left:20px;margin:0 0 34px}ul.also li{margin:0 0 12px}
.cta{display:inline-block;border:2px solid var(--ink);padding:10px 16px;font:600 14px/1 "IBM Plex Mono",monospace;text-decoration:none;margin:0 12px 12px 0}
.cta:hover{background:var(--ink);color:var(--bg)}
.fine{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);font-size:14px;color:var(--muted)}
.entry{border-top:1px solid var(--rule);padding:18px 0}
.entry h3{font:600 19px/1.3 "Source Serif 4",serif;margin:0 0 4px}
.entry .m{font:400 12px/1.5 "IBM Plex Mono",monospace;color:var(--muted);margin:0 0 8px}
.entry p{margin:0 0 8px}
.entry ul{margin:0;padding-left:20px;font-size:16px}.entry li{margin:0 0 6px}
.pill{display:inline-block;font:600 11px/1 "IBM Plex Mono",monospace;letter-spacing:.08em;text-transform:uppercase;padding:4px 6px;border:1.5px solid currentColor;border-radius:4px;margin-right:6px}
.pill.s3{color:var(--red)}
details{margin-top:6px;font-size:15px}summary{cursor:pointer;color:var(--muted);font:600 12px/1.6 "IBM Plex Mono",monospace}
details blockquote{font-size:14px;margin-top:8px}
footer{max-width:680px;margin:0 auto;padding:0 16px 40px;font:400 12px/1.6 "IBM Plex Mono",monospace;color:var(--muted)}
"""




def tally(c: dict) -> str:
    """ "2 baffling and 18 hard to respond to", without a leading "0 baffling"."""
    hard = f'{c["severity2"]} hard to respond to'
    return f'{c["severity3"]} baffling and {hard}' if c["severity3"] else f"{hard} (none rated baffling)"

SMALL = {"of", "the", "and", "for", "in", "on", "at", "to", "a"}
KEEP = {"US", "U.S.", "USA", "DOD", "VA", "DHS", "GSA", "NASA", "IHS", "DLA", "NAVSUP", "FBI", "BOP"}


def name(s: str) -> str:
    """SAM's upper-case names in title case, keeping small words and acronyms: "STATE, DEPARTMENT OF" -> "State, Department of"."""
    words = (s or "").split()
    return " ".join(w if w.upper() in KEEP else w.lower() if i and w.lower() in SMALL else w.capitalize()
                    for i, w in enumerate(words))

def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def safe_url(u) -> str | None:
    u = str(u or "").strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u) else None


def date_label(d: str) -> str:
    x = dt.date.fromisoformat(d[:10])
    return f"{x:%b} {x.day}, {x.year}"


def window_label(w: dict) -> str:
    a, b = date_label(w["from"]), date_label(w["to"])
    return a if a == b else f"{a} – {b}"


def deadline_label(p: dict) -> str:
    if not p.get("deadline"):
        return "no deadline given"
    return date_label(p["deadline"])


def days_label(n: float) -> str:
    if n < 0:
        return f"the deadline was {abs(n):g} days before it was posted"
    if n < 1:
        return f"bidders got {round(n * 24):g} hours to respond"
    return f"bidders got {n:g} days to respond"


def source_label(ev: dict) -> str:
    if ev["source"] == "posting":
        return "SAM.gov posting text"
    return ev["source"] + (f", p. {ev['page']}" if ev.get("page") else "")


def evidence_html(x: dict, p: dict) -> str:
    if not x["evidence"]:  # rushed: a computed fact, not a quote
        return (f'<p class="fact">Posted {e(date_label(p["first_posted"]))}; due {e(deadline_label(p))}: '
                f'{e(days_label(p["window_days"]))}.</p>')
    return "".join(f'<blockquote>{e(ev["quote"])}<span class="src">{e(source_label(ev))}</span></blockquote>'
                   for ev in x["evidence"])


def page(title: str, body: str, *, root: str = "", description: str = TAGLINE) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<meta name="referrer" content="no-referrer">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<link rel="stylesheet" href="{root}style.css">
<link rel="alternate" type="application/rss+xml" title="{e(TITLE)}" href="{root}feed.xml">
</head>
<body>
<header class="top"><a href="{root}index.html">{e(TITLE)}</a>
<nav><a href="{root}past.html">Past weeks</a><a href="{root}about.html">About</a><a href="{root}feed.xml">RSS</a></nav></header>
<main>
{body}
</main>
<footer>Picked and summarized by Claude; every quote is checked against the posting's documents and every
claim by a second, independent check. Postings get amended: check the live notice before relying on anything.
<a href="{ISSUE_URL}">Report a problem</a> · <a href="{PROJECT_URL}">Code and data</a></footer>
</body>
</html>
"""


def winner_body(issue: dict, root: str) -> str:
    w = issue["winner"]
    hero = w["hero"]
    figs = [f'<figure class="hero">{evidence_html(hero, w)}<figcaption><strong>{e(hero["why"])}</strong></figcaption></figure>']
    for x in w["support"]:
        figs.append(f'<figure>{evidence_html(x, w)}<figcaption><strong>{e(x["why"])}</strong></figcaption></figure>')
    also = ""
    if w["also"]:
        items = []
        for x in w["also"]:
            q = "; ".join(f'“{e(ev["quote"])}” ({e(source_label(ev))})' for ev in x["evidence"])
            items.append(f"<li>{e(x['why'])}" + (f'<span class="src">{q}</span>' if q else "") + "</li>")
        also = f'<h2>Also in the documents</h2><ul class="also">{"".join(items)}</ul>'
    link = safe_url(w["link"])
    who = " · ".join(x for x in [name(w["office"]), name(w["agency"])] if x)
    n = len(issue["list"])
    return f"""<div class="label">Week of {e(window_label(issue["window"]))}</div>
<div class="stamp">BAFFLING</div>
<h1>{e(w["headline"])}</h1>
<p class="dek">{e(w["dek"])}</p>
<p class="meta">{e(who)} · {e(w["type"])} {e(w["sol"])} · Posted {e(date_label(w["first_posted"]))} · Due {e(deadline_label(w))}</p>
{"".join(figs)}
{also}
{f'<a class="cta" href="{e(link)}">Read the posting on SAM.gov →</a>' if link else ""}<a class="cta" href="{root}weeks/{e(issue["date"])}-all.html">All {n} flagged this week →</a>
<p class="fine">Picked from the {issue["counts"]["new"]:,} solicitations, RFQs, requests for information and presolicitations
first posted to SAM.gov {e(window_label(issue["window"]))}. <a href="{root}about.html">How it works</a>.</p>"""


def list_body(issue: dict) -> str:
    c = issue["counts"]
    out = [f'<div class="label">Week of {e(window_label(issue["window"]))}</div>',
           f'<h1>Everything flagged</h1>',
           f'<p class="dek">{tally(c)}, out of {c["new"]:,} new postings.</p>',
           f'<p class="meta">Each one survived both checks. “Baffling” and “hard to respond to” are Claude\'s ratings.</p>']
    for x in issue["list"]:
        r = x["pass2"]
        link = safe_url(x["link"])
        title = f'<a href="{e(link)}">{e(x["title"])}</a>' if link else e(x["title"])
        lvl = '<span class="pill s3">Baffling</span>' if r["severity"] == 3 else '<span class="pill">Hard to respond to</span>'
        items = []
        for i in r["issues"]:
            quotes = "".join(f'<blockquote>{e(ev["quote"])}<span class="src">{e(source_label(ev))}</span></blockquote>'
                             for ev in i["evidence"])
            items.append(f"<li><strong>{e(KIND_LABEL[i['kind']])}:</strong> {e(i['why'])}"
                         + (f"<details><summary>Quotes</summary>{quotes}</details>" if quotes else "") + "</li>")
        window = "" if x["window_days"] is None else f" · {days_label(x['window_days'])}"
        out.append(f"""<div class="entry"><h3>{title}</h3>
<p class="m">{lvl}{e(name(x["agency"]))} · {e(x["type"])} {e(x["sol"])} · posted {e(date_label(x["first_posted"]))}{e(window)}</p>
{f'<p>{e(r["headline"])}</p>' if r["headline"] else ""}<ul>{"".join(items)}</ul></div>""")
    return "\n".join(out)


def about_body() -> str:
    return f"""<h1>About</h1>
<p class="dek">{e(TAGLINE)}.</p>
<h2>How it works</h2>
<p>Every day, every new solicitation, request for quotes, request for information (sources sought) and presolicitation
first posted to SAM.gov the day before is read by Claude from its posting text. The ones that look wrong are read again
with their attached documents. A posting is flagged when a capable company could not tell what is wrong, when the
documents contradict themselves, contain text from a different purchase or leftover drafting notes, ask for something
no new bidder could do, or give too little time to respond <em>for what is being bought</em>.</p>
<h2>What is checked</h2>
<ul class="also">
<li>Every quote is checked by code against the file it names, and given its page number.</li>
<li>A contradiction is shown only with both sides quoted.</li>
<li>Every explanation and headline is checked by a second, separate Claude reading of the full documents, told to reject
anything they do not plainly show. Anything that fails is left out, never shown in a weaker form.</li>
<li>Before the weekly page is published, the posting is checked live on SAM.gov: cancelled postings are skipped, and
amended ones have their quotes re-checked against the new version.</li>
<li>Dates, deadlines, agencies and solicitation numbers come straight from the SAM.gov record. The days to respond are
counted from the first time the solicitation was posted, so an amendment does not look rushed.</li>
</ul>
<h2>Limits</h2>
<ul class="also">
<li>“Baffling” is a judgment about the documents as posted, not about the people who wrote them.</li>
<li>Spreadsheets, old Word files, zip files and links to other sites are not read; Claude is told which ones it could
not see and may not call something missing that could be in them.</li>
<li>A posting that left SAM.gov's daily public file before it was read is missed.</li>
<li>Claude sometimes declines to rate a posting; those are counted, not shown.</li>
</ul>
<p>Built from SAM.gov's public Contract Opportunities data. <a href="{PROJECT_URL}">Code and data</a> ·
<a href="{ISSUE_URL}">Report a problem</a></p>"""


def feed(issues: list[dict]) -> str:
    items = []
    for i in reversed(issues[-20:]):
        when = dt.datetime.fromisoformat(i["built_utc"])
        url = f"{SITE_URL}/weeks/{i['date']}.html"
        items.append(f"<item><title>{e(i['winner']['headline'])}</title><link>{e(url)}</link><guid>{e(url)}</guid>"
                     f"<pubDate>{format_datetime(when)}</pubDate><description>{e(i['winner']['dek'])}</description></item>")
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0"><channel><title>{e(TITLE)}</title>'
            f"<link>{SITE_URL}/</link><description>{e(TAGLINE)}</description>{''.join(items)}</channel></rss>\n")


def build(out: Path) -> dict:
    if out.name != "dist":
        raise SystemExit("refusing to build into a directory not named dist")
    issues = [json.loads(p.read_text()) for p in sorted(ISSUES.glob("*.json"))]
    if out.exists():
        shutil.rmtree(out)
    (out / "weeks").mkdir(parents=True)
    (out / "style.css").write_text(CSS)
    for i in issues:
        t = f"{i['winner']['headline']} · {TITLE}"
        (out / "weeks" / f"{i['date']}.html").write_text(page(t, winner_body(i, "../"), root="../", description=i["winner"]["dek"]))
        (out / "weeks" / f"{i['date']}-all.html").write_text(page(f"Everything flagged, week of {window_label(i['window'])}",
                                                                   list_body(i), root="../"))
    if issues:
        latest = issues[-1]
        (out / "index.html").write_text(page(TITLE, winner_body(latest, ""), description=latest["winner"]["dek"]))
    else:
        (out / "index.html").write_text(page(TITLE, "<h1>The first week is coming.</h1>"))
    past = "".join(f'<div class="entry"><p class="m">Week of {e(window_label(i["window"]))}</p>'
                   f'<h3><a href="weeks/{e(i["date"])}.html">{e(i["winner"]["headline"])}</a></h3>'
                   f'<p class="m"><a href="weeks/{e(i["date"])}-all.html">All {len(i["list"])} flagged that week</a></p></div>'
                   for i in reversed(issues))
    (out / "past.html").write_text(page(f"Past weeks · {TITLE}", f"<h1>Past weeks</h1>{past or '<p>None yet.</p>'}"))
    (out / "about.html").write_text(page(f"About · {TITLE}", about_body()))
    (out / "feed.xml").write_text(feed(issues))
    (out / "404.html").write_text(page("Not found", '<h1>Not found</h1><p><a href="/index.html">This week\'s Baffling RFP</a></p>', root="/"))
    return {"issues": len(issues), "out": str(out)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="baffling.site")
    ap.add_argument("--out", default="site/dist")
    print(json.dumps(build(Path(ap.parse_args(argv).out))))


if __name__ == "__main__":
    main()

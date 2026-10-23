"""Claude's reads of each day's new postings, and the checks on them.

Four stages, each sent as one Message Batch (half price; see batch.py):
  prescreen  every posting, text only, cheap model; 1+ goes on
  pass1      those, text only (FIRST_MODEL); 2+ goes on
  pass2      those, one at a time, with their attachments (SECOND_MODEL)
  verify     pass-2 results still rated 2+, fresh context (VERIFY_MODEL)

Why a pre-screen and not the cheap model alone: tested on 2026-10-19..21
(1,493 postings), Haiku rated 2+ missed 7 of the 80 postings that ended on the
list (one was the week's winner) and flagged 505 for the expensive read;
Haiku 1+ caught all 80 and screens out about 40%.

Every pass-2 issue except "rushed" must quote its source verbatim, and a
"contradictory" issue must quote both sides. `locate` checks each quote in code
(whitespace, case, HTML entities, curly/straight quotes and dash variants
ignored) and finds the PDF page; every number in an explanation must appear in
its quotes or the posting facts. Then the verifier checks the headline and
every issue's "why" against the full documents. Anything that fails is moved to
`dropped_issues` with the reason, never shown. A posting with no surviving
issue is set to severity 0; if the verifier call fails, the posting is set to 0
(fail closed).
"""

from __future__ import annotations

import html
import json
import os
import re

PRESCREEN_MODEL = os.environ.get("BAFFLING_PRESCREEN_MODEL", "claude-haiku-5-5")
FIRST_MODEL = os.environ.get("BAFFLING_FIRST_MODEL", "claude-sonnet-5-5")
SECOND_MODEL = os.environ.get("BAFFLING_SECOND_MODEL", "claude-sonnet-5-5")
VERIFY_MODEL = os.environ.get("BAFFLING_VERIFY_MODEL", "claude-opus-5-5")
BATCH = 12
ATTACHMENT_CHARS = 60000  # attachment text sent per posting in pass 2 and verify
KINDS = ["unclear", "contradictory", "wrong_content", "impossible", "rushed"]
# USD per million tokens, standard (not batch) list prices; Haiku's is for
# prompts up to 100K tokens, which every request here is
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}

SEVERITY = ("severity: 0 fine; 1 minor (typos, a misspelled name or email, a mislabeled notice type, or leftover "
            "boilerplate that does not change what is being bought or how to respond); 2 a careful bidder could not "
            "respond properly or would likely price or deliver the wrong thing; 3 baffling: a reasonable reader cannot "
            "tell what the government wants, or the documents are plainly for something else.")

PROBLEMS = """Problems that count:
- unclear: you cannot tell what is being bought, how much, where, or what a response must contain
- contradictory: the posting contradicts itself or its attachments (dates, quantities, set-aside, scope, item)
- wrong_content: text copied from a different buy, unfilled template placeholders, drafting notes left in, garbled or cut-off text
- impossible: requirements no new bidder could meet as written
- rushed: the response window is short FOR WHAT IS BEING BOUGHT. A few days to quote a stock part or a commodity is normal; a few days for construction, ship repair, a complex service or an IT system is not. A deadline before the posting date counts.

Not problems: stray encoding characters (e.g. ���); terse routine parts buys (NSNs) with a clear title; ordinary sole-source notices that clearly say what and why.

About the text you see: the posting text comes from SAM.gov's data file, which strips line breaks, tables and formatting, so never flag layout, run-together lines or missing breaks in it. Attachment text is machine-extracted from PDFs and Word files: odd spacing, broken words and garbled strings (especially from scanned pages or form boxes) may be extraction artifacts, not the document, so do not flag them as garbled. Each PDF page starts with a [page N] marker (never quote the markers). A page marked "scanned, read by OCR" may have wrong spelling, numbers, punctuation and layout: use it to understand the documents, but never flag anything on the strength of its exact wording or formatting, and do not quote it, since quotes from OCR pages are not accepted as evidence. A page marked "could not be read" has unknown content. Never claim that a file or page is blank, empty or unreadable, or that something is missing from a document, because of what you cannot see: we may have failed to read it."""

PASS1 = """You review new U.S. federal contracting postings (solicitations, RFQs, RFIs/sources sought, presolicitations) from SAM.gov, as a capable company deciding whether it could respond.

{problems}
Also note when the posting text says almost nothing and refers to attachments (needs_attachments); that alone is not a problem, the attachments are read later.

Use only the posting text and facts given. {severity}

Postings:
{records}"""

PASS2 = """You review one new U.S. federal contracting posting from SAM.gov, with its attachments, as a capable company deciding whether it could respond.

{problems}
A posting whose text says "see attached" is fine if the attachments are clear.

Rules:
- Use only what is below; no outside knowledge about agencies, vendors, places or prices.
- Each issue gives evidence: one or more quotes, each naming its source ("posting" or an attachment's exact file name as shown) and copied from it verbatim, 5-40 words. A contradictory issue gives BOTH sides as two separate quotes. A rushed issue may give no quote (the window is a computed fact).
- why: one plain sentence a reader who is not a contracting expert would understand, saying what the documents say that is wrong. Facts only, no sarcasm. Do not claim consequences ("bidders cannot tell", "no one could") unless the documents make them certain; describe what is there. Every number in it must appear in the quotes or the facts.
- {severity}
- headline: one plain factual sentence saying what is strange about this posting.

Facts: {facts}

Posting text:
{text}

Attachments:
{attachments}"""

PASS1_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ratings"], "properties": {"ratings": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["index", "severity", "summary", "needs_attachments"], "properties": {
                                   "index": {"type": "integer"}, "severity": {"type": "integer"},
                                   "summary": {"type": "string"}, "needs_attachments": {"type": "boolean"}}}}}}

PASS2_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["severity", "headline", "issues"], "properties": {
    "severity": {"type": "integer"}, "headline": {"type": "string"},
    "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["kind", "why", "evidence"], "properties": {
                                              "kind": {"type": "string", "enum": KINDS},
                                              "why": {"type": "string"},
                                              "evidence": {"type": "array", "items": {
                                                  "type": "object", "additionalProperties": False,
                                                  "required": ["source", "quote"], "properties": {
                                                      "source": {"type": "string"}, "quote": {"type": "string"}}}}}}}}}

VERIFY = """You are checking another reviewer's claims about one U.S. federal contracting posting before they are published. Be strict: a claim is supported only if the posting facts, posting text and attachment text below plainly show it is true. Reject a claim that:
- goes beyond what the documents say, or relies on outside knowledge
- says something is missing when it might be in a part of the documents you cannot see (attachments marked "not read" or "cut")
- misreads a quote or takes it out of context (e.g. a sentence that is fine in context)
- includes any detail (a number, date, count, name or duration) that the documents do not show
- claims a consequence ("bidders cannot tell", "no one could") the documents do not make certain
- exaggerates
- calls text garbled, run together or badly formatted: the posting text has had its formatting stripped by SAM.gov's data file, and attachment text is machine-extracted, so such problems may not be in the real documents
- rests on a page marked "scanned, read by OCR" (OCR misreads numbers and punctuation) or on what cannot be seen: pages marked "could not be read", links and files not read

Then, counting only the claims you marked supported, rate the posting. {severity}

Claims (each is about this posting):
{claims}

Facts: {facts}

Posting text:
{text}

Attachments:
{attachments}"""

VERIFY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdicts", "severity"], "properties": {
    "severity": {"type": "integer"}, "verdicts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["id", "supported", "reason"], "properties": {
                                   "id": {"type": "string"}, "supported": {"type": "boolean"},
                                   "reason": {"type": "string"}}}}}}


def client():
    import anthropic
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)


def params(model: str, prompt: str, schema: dict, max_tokens: int) -> dict:
    return {"model": model, "max_tokens": max_tokens,
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": prompt}]}


def parse(message) -> tuple[dict | None, str, list[int]]:
    """(parsed JSON or None, stop_reason, [input, output tokens])."""
    tok = [message.usage.input_tokens, message.usage.output_tokens]
    texts = [b.text for b in message.content if b.type == "text"]
    if message.stop_reason == "end_turn" and texts:
        try:
            return json.loads(texts[0]), message.stop_reason, tok
        except ValueError:
            return None, "invalid_json", tok
    return None, message.stop_reason, tok


def cost(model: str, tokens: list[int], *, batch: bool) -> float:
    i, o = PRICES[model]
    return (tokens[0] * i + tokens[1] * o) / 1e6 * (0.5 if batch else 1.0)


def call(c, p: dict) -> tuple[dict | None, str, list[int]]:
    """One direct (not batched) request, retried once; used for the weekly pick."""
    tok = [0, 0]
    for _ in range(2):
        out, stop, t = parse(c.messages.create(**p))
        tok = [tok[0] + t[0], tok[1] + t[1]]
        if out is not None:
            return out, stop, tok
    return None, stop, tok


def window_text(p: dict) -> str:
    if p["window_days"] is None:
        return "no deadline given"
    return f"{p['window_days']} days from first posting ({p['first_posted'][:16]} ET) to deadline ({p['deadline']})"


def facts(p: dict) -> str:
    return (f"{p['type']} | title: {p['title']} | {p['agency']} / {p['subtier']} / {p['office']} | sol# {p['sol']} | "
            f"NAICS {p['naics']} PSC {p['psc']} | set-aside: {p['set_aside'] or 'none'} | place: {p['place'] or 'n/a'} | "
            f"window: {window_text(p)}")


def pass1_requests(postings: list[dict], model: str, prefix: str) -> dict[str, dict]:
    """One request per BATCH postings; custom_id = prefix-start."""
    out = {}
    for start in range(0, len(postings), BATCH):
        chunk = postings[start:start + BATCH]
        recs = "\n".join(f"[{start + j}] {facts(p)}\nText: {p['description']}\n" for j, p in enumerate(chunk))
        out[f"{prefix}-{start}"] = params(model, PASS1.format(problems=PROBLEMS, severity=SEVERITY, records=recs),
                                          PASS1_SCHEMA, 12000)
    return out


def pass1_valid(cid: str, parsed: dict, n: int) -> bool:
    """A pass-1 reply must rate exactly the postings it was sent."""
    start = int(cid.rsplit("-", 1)[1])
    want = set(range(start, min(start + BATCH, n)))
    return {o["index"] for o in parsed["ratings"]} == want


def pass1_collect(results: dict, n: int) -> list[dict | None]:
    """Ratings by posting index; None where the request failed after its retry."""
    out: list[dict | None] = [None] * n
    for cid, (parsed, _stop, _tok) in results.items():
        if parsed is not None and pass1_valid(cid, parsed, n):
            for o in parsed["ratings"]:
                out[o["index"]] = o
    return out


_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        " ": " ", "‐": "-", "‑": "-"})


def norm(s: str) -> str:
    t = re.sub(r"\s+", " ", html.unescape(s or "").translate(_TRANS))
    # HTML-to-text leaves spaces before punctuation ("Quantity : 150", "10:00am ."); drop them on both sides
    t = re.sub(r"\s+([.,;:!?)\]])", r"\1", re.sub(r"([(\[])\s+", r"\1", t))
    return t.strip().casefold()


def locate(quote: str, source: str, posting: dict, files: list[dict], allow_ocr: bool = False) -> dict | None:
    """Where the quote is: {"source", "page"} (page None for the posting or a .docx), or None.
    Text read by OCR is not searched unless allow_ocr: OCR misreads (it turned "$423,655,338"
    into "$423,655.338"), so a quote from it is not known to be in the document."""
    q = norm(quote)
    if not q:
        return None
    if source == "posting":
        return {"source": "posting", "page": None} if q in norm(posting["title"] + " \n " + posting["description"]) else None
    for f in files:
        if f["name"] != source or "pages" not in f:
            continue
        pdf = f["name"].lower().endswith(".pdf")
        bad = set() if allow_ocr else set(f.get("ocr_pages", ()))
        pages = [("" if i + 1 in bad else pg) for i, pg in enumerate(f["pages"])]
        for i, pg in enumerate(pages):
            if q in norm(pg):
                return {"source": f["name"], "page": i + 1 if pdf else None}
        # quote running across a page break
        for i in range(len(pages) - 1):
            if pages[i] and pages[i + 1] and q in norm(pages[i] + " " + pages[i + 1]):
                return {"source": f["name"], "page": i + 1 if pdf else None}
    return None


def _nums(text: str) -> list[str]:
    return [n.rstrip(".,").replace(",", "").lstrip("0") or "0" for n in re.findall(r"\d[\d,.]*", text)]


def numbers_supported(text: str, haystack: str) -> bool:
    """Every number in `text` appears in `haystack` (leading zeros and thousands
    separators ignored; a decimal in the haystack also allows its rounded,
    floor and ceiling whole numbers, e.g. 7.13 days -> "7 days")."""
    have = set()
    for n in _nums(haystack):
        have.add(n)
        try:
            f = float(n)
        except ValueError:
            continue
        have.update({str(int(f)), str(round(f)), str(int(f) + (f % 1 > 0))})
    return all(n in have for n in _nums(text))


def haystack(posting: dict, files: list[dict], evidence: list[dict]) -> str:
    pages = " ".join(f"{f['name']} ({len(f['pages'])} pages)" for f in files if "pages" in f)
    return " ".join([facts(posting), posting["title"], pages] + [e["quote"] for e in evidence])


def _page_marker(f: dict, i: int) -> str:
    n = i + 1
    if n in f.get("unread_pages", ()):
        return f"[page {n}: an image that could not be read; its content is unknown]\n"
    if n in f.get("ocr_pages", ()):
        return f"[page {n}: scanned, read by OCR]\n"
    return f"[page {n}]\n"


# A claim that a file or page is blank or unreadable can come from our own failed text
# extraction (scanned pages), so it is never shown. Nor, when anything went unread (a
# link, a spreadsheet, a page even OCR could not read), is a claim that something is
# absent from the documents: it may be in what we did not read.
_DOC = r"(page|file|document|attachment|pdf|sow|statement of work)s?"
UNREADABLE_CLAIM = re.compile(
    rf"\b(un|not |no |il)(readable|legible)|no (readable |extractable )?text|cannot be read|could not be read"
    rf"|\b(blank|empty) {_DOC}\b|\b{_DOC} (is |are |appears? |seems? )(to be )?(blank|empty)\b|jumps from page"
    rf"|\b(pages?|text|content) (is |are )?(missing|cut off)\b", re.I)
ABSENCE_CLAIM = re.compile(
    r"missing from|absent from|not (included|present|provided|shown|listed) (in|anywhere)"
    r"|(does|do) not (include|contain|list|say|state|mention|give|provide|specify)|\bnever (says|states|mentions|gives|appears)"
    r"|\b(gives|has|lists|states|says|includes) no\b|\bwithout (saying|stating|giving|any)\b"
    r"|\bnot (stated|specified)\b|\bno (quantity|quantities|scope|specifications?|instructions)\b", re.I)


def anything_unread(files: list[dict], error: str | None) -> bool:
    return bool(error) or any("skipped" in f or f.get("unread_pages") for f in files)


def unreadable_claim(text: str, files: list[dict], error: str | None) -> bool:
    return bool(UNREADABLE_CLAIM.search(text) or (anything_unread(files, error) and ABSENCE_CLAIM.search(text)))


def attachment_block(files: list[dict], error: str | None) -> str:
    if error:
        return f"(attachments could not be listed: {error})"
    parts, left = [], ATTACHMENT_CHARS
    for f in files:
        if "pages" in f and left > 0:
            text = "\n".join(_page_marker(f, i) + pg for i, pg in enumerate(f["pages"])) \
                if f["name"].lower().endswith(".pdf") else "\n".join(f["pages"])
            t = text[:left]
            left -= len(t)
            parts.append(f"--- {f['name']} ({len(text)} chars{', cut' if len(t) < len(text) else ''}) ---\n{t}")
        else:
            parts.append(f"--- {f['name']}: not read ({f.get('skipped', 'over the length cap')}) ---")
    return "\n".join(parts) or "(none)"


def pass2_request(posting: dict, att: dict) -> dict:
    prompt = PASS2.format(problems=PROBLEMS, severity=SEVERITY, facts=facts(posting), text=posting["description"],
                          attachments=attachment_block(att.get("files", []), att.get("error")))
    return params(SECOND_MODEL, prompt, PASS2_SCHEMA, 6000)


def pass2_apply(posting: dict, att: dict, out: dict | None, stop: str, tok: list[int]) -> dict:
    """Code checks on a pass-2 reply. Returns the result (verify not yet run)."""
    files = att.get("files", [])
    if out is None:
        return {"not_rated": stop, "tokens": tok}
    kept, dropped = [], []
    for x in out["issues"]:
        ev, missing = [], []
        for q in x["evidence"]:
            where = locate(q["quote"], q["source"], posting, files)
            (ev.append({"quote": q["quote"], **where}) if where else missing.append(q))
        if missing and all(locate(q["quote"], q["source"], posting, files, allow_ocr=True) for q in missing):
            dropped.append({**x, "reason": "quote is from a scanned page read by OCR, which may misread it"})
        elif missing:
            dropped.append({**x, "reason": "quote not found in the named source", "not_found": missing})
        elif x["kind"] == "contradictory" and len(ev) < 2:
            dropped.append({**x, "reason": "a contradiction needs both sides quoted"})
        elif x["kind"] != "rushed" and not ev:
            dropped.append({**x, "reason": "no quote"})
        elif x["kind"] == "rushed" and posting["window_days"] is None:
            dropped.append({**x, "reason": "rushed, but the posting has no deadline"})
        elif unreadable_claim(x["why"], files, att.get("error")):
            dropped.append({**x, "reason": "claims something is unreadable or absent, which we cannot establish"})
        elif not numbers_supported(x["why"], haystack(posting, files, ev)):
            dropped.append({**x, "reason": "a number in the explanation is not in its quotes or the posting facts"})
        else:
            kept.append({"kind": x["kind"], "why": x["why"], "evidence": ev})
    # the headline may summarize a dropped issue, so any drop blanks it (the weekly pick writes and verifies its own)
    if dropped or unreadable_claim(out["headline"], files, att.get("error")) or not numbers_supported(
            out["headline"], haystack(posting, files, [e for x in kept for e in x["evidence"]])):
        out["headline"] = ""
    return {"severity": out["severity"] if kept else 0, "headline": out["headline"], "issues": kept,
            "dropped_issues": dropped, "tokens": tok,
            "attachments": [{k: v for k, v in f.items() if k != "pages"} | ({"read": True} if "pages" in f else {})
                            for f in files],
            "attachments_error": att.get("error")}


def verify_request(posting: dict, files: list[dict], error: str | None, result: dict) -> dict:
    claims = ([f"[headline] {result['headline']}"] if result["headline"] else []) + [
        f"[issue{i}] ({x['kind']}) {x['why']}\n    quoted: " + " | ".join(
            f"{e['source']}{' p.' + str(e['page']) if e['page'] else ''}: \"{e['quote']}\"" for e in x["evidence"])
        for i, x in enumerate(result["issues"])]
    prompt = VERIFY.format(claims="\n".join(claims), severity=SEVERITY, facts=facts(posting),
                           text=posting["description"], attachments=attachment_block(files, error))
    return params(VERIFY_MODEL, prompt, VERIFY_SCHEMA, 4000)


def verify_apply(result: dict, out: dict | None, stop: str, tok: list[int]) -> None:
    """Apply the independent check. Unsupported issues move to dropped_issues; an
    unsupported headline is blanked. Severity becomes the lower of the two
    ratings, or 0 if nothing survives; a failed check sets it to 0."""
    result["verify_tokens"] = tok
    if out is None:  # fail closed: nothing unverified is shown
        result.update(verify_failed=stop, severity=0)
        return
    v = {x["id"].strip("[]"): x for x in out["verdicts"]}
    ok = []
    for i, x in enumerate(result["issues"]):
        verdict = v.get(f"issue{i}")
        if verdict and verdict["supported"]:
            ok.append(x)
        else:
            result["dropped_issues"].append({**x, "reason": "verifier: " + (verdict["reason"] if verdict else "no verdict")})
    lost = len(ok) < len(result["issues"])
    result["issues"] = ok
    hv = v.get("headline")
    if not (hv and hv["supported"]):
        result["dropped_headline"] = {"headline": result["headline"], "reason": hv["reason"] if hv else "no verdict"}
        result["headline"] = ""
    elif lost and result["headline"]:  # it may summarize a rejected issue
        result["dropped_headline"] = {"headline": result["headline"], "reason": "an issue it may summarize was rejected"}
        result["headline"] = ""
    # severity can only go down: the verifier's rating of what survived
    result["severity"] = min(result["severity"], out["severity"]) if ok else 0

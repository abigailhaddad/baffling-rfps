import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from baffling import attachments, issue, rate, sam, site

HEADER = ["NoticeId", "Title", "Sol#", "Department/Ind.Agency", "Sub-Tier", "Office", "PostedDate", "Type",
          "ResponseDeadLine", "NaicsCode", "ClassificationCode", "SetASide", "PopCity", "PopState", "Description"]


def row(nid, sol, posted, typ="Solicitation", due="2026-11-01T17:00:00-04:00", title="Snow removal", desc="Remove snow."):
    return dict(zip(HEADER, [nid, title, sol, "STATE, DEPARTMENT OF", "STATE", "EMBASSY", posted, typ, due, "561730",
                             "S208", "", "Bucharest", "RO", desc]))


def write_extract(path: Path, rows: list[dict]) -> None:
    lines = [",".join(f'"{h}"' for h in HEADER)]
    for r in rows:
        lines.append(",".join('"' + r[h].replace('"', '""') + '"' for h in HEADER))
    path.write_bytes("\r\n".join(lines).encode("cp1252"))


def test_extract_is_read_as_windows_1252(tmp_path):
    p = tmp_path / "x.csv"
    write_extract(p, [row("a", "S1", "2026-10-21 10:00:00", desc="Contact Mr. Howard “Toby” Doe – now")])
    assert sam.read_rows(p)[0]["Description"] == "Contact Mr. Howard “Toby” Doe – now"


def test_new_postings_window_from_first_version_and_seen_and_types():
    gs = sam.groups([
        row("a1", "S1", "2026-10-19 09:00:00", due="2026-10-30T17:00:00-04:00"),
        row("a2", "s1", "2026-10-21 09:00:00", due="2026-11-02T17:00:00-04:00"),  # amendment, same Sol# any case
        row("b", "S2", "2026-10-21 23:20:00"),
        row("c", "S3", "2026-10-21 08:00:00", typ="Award Notice"),
        row("d", "S4", "2026-10-22 01:00:00"),  # tomorrow: not yet
        row("e", "S5", "2026-10-20 23:45:00"),  # posted late yesterday, first seen in today's file
    ])
    seen = {}
    sam.mark_seen(seen, gs, before=dt.date(2026, 10, 20))  # S1 was seen before
    new, skipped = sam.new_postings(gs, dt.date(2026, 10, 21), seen)
    assert [p["sol"] for p in new] == ["S5", "S2"]
    assert skipped == {"not_solicitation_type": 1, "late": 1, "deadline_before_first_posting": 0}
    s1 = sam.posting(gs["sol:S1"])
    assert s1["first_notice_id"] == "a1" and s1["notice_id"] == "a2"
    assert s1["window_days"] == 14.33  # 14 days 8 hours, from the FIRST posting to the latest deadline
    assert s1["link"] == "https://sam.gov/opp/a2/view"


def test_bare_date_deadline_is_end_of_day_and_past_deadlines_are_skipped():
    assert sam.deadline({"ResponseDeadLine": "2026-10-21"}).isoformat() == "2026-10-21T23:59:00-04:00"
    old = row("n1", "OLD1", "2026-10-21 13:27:58", due="2026-08-21T11:00:00-04:00", typ="Presolicitation")
    new, skipped = sam.new_postings(sam.groups([old]), dt.date(2026, 10, 21), {})
    assert new == [] and skipped["deadline_before_first_posting"] == 1


def test_offsetless_deadline_is_eastern():
    p = sam.posting([row("a", "S1", "2026-10-21 09:00:00", due="2026-10-22T09:00:00")])
    assert p["window_days"] == 1.0


FILES = [{"name": "RFQ.pdf", "pages": ["Page one text.", "The Government should\nspecify the unit for “final”",
                                       "snow disposal. End."]},
         {"name": "SOW.docx", "pages": ["PERFORMANCE WORK STATEMENT (PWS) – GARDENING SERVICES"]},
         {"name": "sheet.xlsx", "skipped": "format not read"}]
POSTING = {"title": "Snow removal", "description": "Quotes due Nov 4.  See   attached &amp; RFQ.", "window_days": 14.0,
           "first_posted": "2026-10-21T08:54:52-04:00", "deadline": "2026-11-04T17:00:00+03:00", "type": "Solicitation",
           "agency": "STATE, DEPARTMENT OF", "subtier": "STATE", "office": "U.S. EMBASSY BUCHAREST", "sol": "19R",
           "naics": "56173", "psc": "S208", "set_aside": "", "place": "Bucharest", "link": "https://sam.gov/opp/x/view",
           "notice_id": "x", "key": "sol:19R"}


@pytest.mark.parametrize("quote,source,want", [
    ("see attached & RFQ", "posting", {"source": "posting", "page": None}),
    ('the government should specify the unit for "final"', "RFQ.pdf", {"source": "RFQ.pdf", "page": 2}),
    ('unit for "final" snow disposal', "RFQ.pdf", {"source": "RFQ.pdf", "page": 2}),  # across a page break
    ("PWS) - GARDENING SERVICES", "SOW.docx", {"source": "SOW.docx", "page": None}),
    ("GARDENING SERVICES", "RFQ.pdf", None),  # real text, wrong file
    ("Quotes due Nov 5", "posting", None),
    ("anything", "sheet.xlsx", None),  # a file that was not read cannot be quoted
    ("   ", "posting", None),
])
def test_locate(quote, source, want):
    assert rate.locate(quote, source, POSTING, FILES) == want


def msg(r):
    """A fake API message: r is a dict (JSON reply) or None (a refusal)."""
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
    if r is None:
        return SimpleNamespace(stop_reason="refusal", content=[], usage=usage)
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(r))], usage=usage)


ATT = {"files": FILES}
GOOD = {"kind": "wrong_content", "why": "The snow-removal work statement is headed Gardening Services.",
        "evidence": [{"source": "SOW.docx", "quote": "GARDENING SERVICES"}]}
INVENTED = {"kind": "unclear", "why": "x", "evidence": [{"source": "RFQ.pdf", "quote": "a sentence that is not there"}]}
ONE_SIDED = {"kind": "contradictory", "why": "x", "evidence": [{"source": "posting", "quote": "Quotes due Nov 4."}]}
RUSHED = {"kind": "rushed", "why": "Fourteen days is short.", "evidence": []}
TOK = [10, 5]


def test_pass2_drops_bad_quotes_and_one_sided_contradictions_then_verifies():
    r = rate.pass2_apply(POSTING, ATT, {"severity": 3, "headline": "Snow contract is a gardening contract.",
                                        "issues": [GOOD, INVENTED, ONE_SIDED, RUSHED]}, "end_turn", TOK)
    assert [x["kind"] for x in r["issues"]] == ["wrong_content", "rushed"]
    assert r["issues"][0]["evidence"][0] == {"quote": "GARDENING SERVICES", "source": "SOW.docx", "page": None}
    req = rate.verify_request(POSTING, FILES, None, r)
    assert req["model"] == rate.VERIFY_MODEL and "GARDENING SERVICES" in req["messages"][0]["content"]
    rate.verify_apply(r, {"severity": 2, "verdicts": [{"id": "headline", "supported": True, "reason": ""},
                                                      {"id": "issue0", "supported": True, "reason": ""},
                                                      {"id": "issue1", "supported": False, "reason": "14 days is normal"}]},
                      "end_turn", TOK)
    assert [x["kind"] for x in r["issues"]] == ["wrong_content"]
    assert [d["reason"] for d in r["dropped_issues"]] == ["quote not found in the named source",
                                                          "a contradiction needs both sides quoted",
                                                          "verifier: 14 days is normal"]
    assert r["severity"] == 2  # the lower of the two ratings


def test_verifier_failure_fails_closed():
    r = rate.pass2_apply(POSTING, ATT, {"severity": 3, "headline": "h", "issues": [GOOD]}, "end_turn", TOK)
    rate.verify_apply(r, None, "refusal", TOK)
    assert r["severity"] == 0 and r["verify_failed"] == "refusal"


def test_unsupported_headline_is_blanked_and_missing_verdict_drops_issue():
    r = rate.pass2_apply(POSTING, ATT, {"severity": 3, "headline": "Invented claim.", "issues": [GOOD]}, "end_turn", TOK)
    rate.verify_apply(r, {"severity": 3, "verdicts": [{"id": "headline", "supported": False, "reason": "not shown"}]},
                      "end_turn", TOK)
    assert r["headline"] == "" and r["dropped_headline"]["reason"] == "not shown"
    assert r["issues"] == [] and r["severity"] == 0


def test_unreadable_and_absence_claims_are_dropped_and_blank_the_headline():
    unreadable = {**GOOD, "why": "The attached SOW contains no readable text."}
    absent = {**GOOD, "why": "The work statement never states the quantity of gardens."}
    read = {"files": FILES[:2]}
    r = rate.pass2_apply(POSTING, read, {"severity": 3, "headline": "Gardening.", "issues": [GOOD, unreadable, absent]},
                         "end_turn", TOK)
    # every file was read, so the absence claim stands; the unreadable claim never does
    assert [x["why"] for x in r["issues"]] == [GOOD["why"], absent["why"]]
    assert r["dropped_issues"][0]["reason"].startswith("claims something is unreadable")
    assert r["headline"] == ""  # it may have summarized the dropped issue
    link = {"files": FILES[:2] + [{"name": "Specs", "type": "link", "skipped": "link"}]}
    r = rate.pass2_apply(POSTING, link, {"severity": 3, "headline": "", "issues": [GOOD, absent]}, "end_turn", TOK)
    assert [x["why"] for x in r["issues"]] == [GOOD["why"]]  # the quantity may be in the link we did not open
    # real template blanks are a quotable problem, not an extraction claim
    assert not rate.unreadable_claim("The clauses still contain blank template fields.", FILES, None)


def test_verifier_rejecting_an_issue_blanks_the_headline():
    r = rate.pass2_apply(POSTING, ATT, {"severity": 3, "headline": "Gardening.", "issues": [GOOD, RUSHED]}, "end_turn", TOK)
    rate.verify_apply(r, {"severity": 3, "verdicts": [{"id": "headline", "supported": True, "reason": ""},
                                                      {"id": "issue0", "supported": True, "reason": ""},
                                                      {"id": "issue1", "supported": False, "reason": "normal"}]},
                      "end_turn", TOK)
    assert r["headline"] == "" and len(r["issues"]) == 1


def test_scanned_pages_are_ocred_and_unreadable_pages_marked(monkeypatch, tmp_path):
    text = "A" * 50
    monkeypatch.setattr(attachments.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=f"{text}\f  \f\f"))
    monkeypatch.setattr(attachments, "ocr_page", lambda path, n: "OCR TEXT " * 10 if n == 2 else "")
    pages, ocr, unread = attachments.pdf_pages(tmp_path / "x.pdf")
    assert pages[0] == text and pages[1].startswith("OCR TEXT") and (ocr, unread) == ([2], [3])
    block = rate.attachment_block([{"name": "x.pdf", "pages": pages, "ocr_pages": ocr, "unread_pages": unread}], None)
    assert "[page 2: scanned, read by OCR]" in block and "[page 3: an image that could not be read" in block


def test_quotes_from_ocr_pages_are_not_evidence():
    f = {"name": "SSJ.pdf", "pages": ["Typed page one.", "TOTAL $2,657,393.210"], "ocr_pages": [2], "unread_pages": []}
    assert rate.locate("Typed page one", "SSJ.pdf", POSTING, [f]) == {"source": "SSJ.pdf", "page": 1}
    assert rate.locate("$2,657,393.210", "SSJ.pdf", POSTING, [f]) is None
    x = {"kind": "wrong_content", "why": "The total uses a decimal point.",
         "evidence": [{"source": "SSJ.pdf", "quote": "TOTAL $2,657,393.210"}]}
    r = rate.pass2_apply(POSTING, {"files": [f]}, {"severity": 2, "headline": "", "issues": [x]}, "end_turn", TOK)
    assert r["issues"] == [] and r["dropped_issues"][0]["reason"].startswith("quote is from a scanned page")


def test_refusal_is_recorded_not_raised():
    assert rate.pass2_apply(POSTING, ATT, None, "refusal", TOK)["not_rated"] == "refusal"


def test_explanation_with_an_unsupported_number_is_dropped():
    bad = {**GOOD, "why": "The 2019 gardening work statement was reused."}
    r = rate.pass2_apply(POSTING, ATT, {"severity": 3, "headline": "h", "issues": [bad]}, "end_turn", TOK)
    assert r["issues"] == [] and r["severity"] == 0
    assert r["dropped_issues"][0]["reason"].startswith("a number in the explanation")


def test_numbers_in_headline_must_come_from_facts_or_quotes():
    iss = [{"evidence": [{"quote": "150 tons", "source": "RFQ.pdf", "page": 7}]}]
    assert issue.numbers_ok("150 tons of snow, 14.0 days", POSTING, iss)
    assert not issue.numbers_ok("$2 million snow contract", POSTING, iss)


def test_one_issue_a_week():
    assert issue.same_week(dt.date(2026, 10, 26), dt.date(2026, 11, 1))
    assert not issue.same_week(dt.date(2026, 10, 25), dt.date(2026, 10, 26))


def test_site_escapes_and_has_no_scripts(tmp_path, monkeypatch):
    ev = {"quote": "<script>alert(1)</script>", "source": "posting", "page": None}
    p = {**POSTING, "title": "<b>Snow</b>", "first_posted": "2026-10-21T08:54:52-04:00"}
    win = {**p, "headline": "Snow & <i>gardens</i>", "dek": "d", "hero": {"kind": "wrong_content", "why": "w", "evidence": [ev]},
           "support": [{"kind": "rushed", "why": "short", "evidence": []}], "also": []}
    data = {"date": "2026-10-26", "window": {"from": "2026-10-19", "to": "2026-10-25"}, "built_utc": "2026-10-26T12:00:00+00:00",
            "counts": {"new": 1000, "pass1_flagged": 1, "severity3": 1, "severity2": 0, "not_rated": 0, "verify_failed": 0,
                       "finalists": 1},
            "winner": win, "list": [{**p, "pass2": {"severity": 3, "headline": "h", "issues": [win["hero"]]}}]}
    d = tmp_path / "issues"
    d.mkdir()
    (d / "2026-10-26.json").write_text(json.dumps(data))
    monkeypatch.setattr(site, "ISSUES", d)
    site.build(tmp_path / "dist")
    for f in (tmp_path / "dist").rglob("*.html"):
        t = f.read_text()
        assert "<script" not in t.lower() and "<i>gardens" not in t and "<b>Snow" not in t
    idx = (tmp_path / "dist" / "index.html").read_text()
    assert "Snow &amp; &lt;i&gt;gardens" in idx
    assert "bidders got 14 days to respond" in idx  # rushed renders the computed window, not a quote
    assert "1,000" in idx


def test_build_refuses_other_dirs(tmp_path):
    with pytest.raises(SystemExit):
        site.build(tmp_path / "site")


class FakeBatchClient:
    """Runs each batch at once with `answer(prompt) -> dict | None`. `bad_first` lists
    prompt prefixes whose first reply is a refusal (to exercise the retry)."""
    def __init__(self, answer, bad_first=()):
        self.answer, self.bad_first, self.batches, self.sent = answer, set(bad_first), {}, []
        self.messages = SimpleNamespace(batches=SimpleNamespace(create=self.create, retrieve=self.retrieve,
                                                                results=self.results, cancel=None))

    def create(self, requests):
        bid = f"b{len(self.batches)}"
        out = []
        for r in requests:
            prompt = r["params"]["messages"][0]["content"]
            self.sent.append((r["params"]["model"], prompt[:20]))
            bad = next((k for k in self.bad_first if prompt.startswith(k)), None)
            if bad:
                self.bad_first.discard(bad)
                reply = None
            else:
                reply = self.answer(prompt)
            out.append(SimpleNamespace(custom_id=r["custom_id"],
                                       result=SimpleNamespace(type="succeeded", message=msg(reply))))
        self.batches[bid] = out
        return SimpleNamespace(id=bid, processing_status="ended")

    def retrieve(self, bid):
        return SimpleNamespace(id=bid, processing_status="ended")

    def results(self, bid):
        return self.batches[bid]


def answer(prompt):
    import re as _re
    if prompt.startswith("You review new"):  # prescreen or pass 1: first posting flagged
        idx = [int(x) for x in _re.findall(r"^\[(\d+)\]", prompt, flags=_re.M)]
        return {"ratings": [{"index": i, "severity": 3 if i == 0 else 0, "summary": "s", "needs_attachments": False}
                            for i in idx]}
    if prompt.startswith("You review one"):  # pass 2
        return {"severity": 3, "headline": "Snow contract is a gardening contract.", "issues": [GOOD]}
    return {"severity": 3, "verdicts": [{"id": "headline", "supported": True, "reason": ""},
                                        {"id": "issue0", "supported": True, "reason": ""}]}


def _daily_setup(tmp_path, monkeypatch, client, cap="3"):
    from baffling import attachments, batch, daily
    ext = tmp_path / "extract.csv"
    write_extract(ext, [row("old", "S0", "2026-10-01 09:00:00"),
                        row("a", "S1", "2026-10-21 09:00:00"), row("b", "S2", "2026-10-21 10:00:00"),
                        row("c", "S3", "2026-10-22 06:00:00")])
    monkeypatch.setattr(daily, "DAYS", tmp_path / "days")
    monkeypatch.setattr(daily, "SEEN", tmp_path / "seen.csv")
    monkeypatch.setattr(daily, "WORK", tmp_path / "work")
    monkeypatch.setattr(rate, "client", lambda: client)
    monkeypatch.setattr(batch, "DAILY_CAP_USD", float(cap))
    monkeypatch.setattr(batch.Budget.__init__, "__defaults__", (float(cap),))
    monkeypatch.setattr(attachments, "fetch", lambda nid, wd, s=None: {"notice_id": nid, "files": FILES})
    daily.main(["--extract", str(ext), "--seed-before", "2026-10-21"])
    daily.main(["--extract", str(ext), "--day", "2026-10-21"])
    return daily, ext, json.loads((tmp_path / "days" / "2026-10-21.json").read_text())


def test_daily_end_to_end(tmp_path, monkeypatch):
    c = FakeBatchClient(answer, bad_first=["You review one"])  # pass 2's first reply is refused, then retried
    daily, ext, d = _daily_setup(tmp_path, monkeypatch, c)
    assert [p["sol"] for p in d["postings"]] == ["S1", "S2"]
    assert d["counts"]["prescreen_passed"] == 1 and d["counts"]["pass1_flagged"] == 1 and d["counts"]["pass2"]["3"] == 1
    assert d["postings"][0]["pass2"]["issues"][0]["evidence"][0]["source"] == "SOW.docx"
    assert d["postings"][1]["pass1"] == {"prescreen": 0, "severity": None, "summary": "s", "failed": False}
    assert "description" not in d["postings"][1]  # descriptions are kept only for postings read twice
    models = [m for m, _ in c.sent]
    assert models == [rate.PRESCREEN_MODEL, rate.FIRST_MODEL, rate.SECOND_MODEL, rate.SECOND_MODEL, rate.VERIFY_MODEL]
    assert d["cost_usd"]["total"] > 0 and set(d["cost_usd"]) >= {"prescreen", "pass1", "pass2", "verify", "cap"}
    seen = sam.load_seen(tmp_path / "seen.csv")
    assert "sol:S1" in seen and "sol:S3" not in seen  # tomorrow's posting stays new for tomorrow
    with pytest.raises(SystemExit):
        daily.main(["--extract", str(ext), "--day", "2026-10-21"])  # never overwrites a day


def test_daily_cap_records_what_did_not_fit(tmp_path, monkeypatch):
    from baffling import batch
    # room for the prescreen and pass 1 but not for pass 2
    monkeypatch.setattr(batch, "estimate", lambda p, stage: 0.0 if stage in ("prescreen", "pass1") else 99.0)
    _, _, d = _daily_setup(tmp_path, monkeypatch, FakeBatchClient(answer))
    p2 = d["postings"][0]["pass2"]
    assert p2["not_rated"] == "daily budget" and d["counts"]["budget_cut"] == 1


def test_locate_ignores_spaces_html_leaves_before_punctuation():
    live = {**POSTING, "description": "Required Quantity : 150 Each Site visit at 10:00am . ( insert emails )"}
    for q in ["Required Quantity: 150 Each", "10:00am.", "(insert emails)"]:
        assert rate.locate(q, "posting", live, [])


@pytest.mark.parametrize("text,hay,ok", [
    ("a 12-to-18-month rental", "Rental of One (1) 500kw Generator 12 Months", False),
    ("a 12-month rental", "Rental of One (1) 500kw Generator 12 Months", True),
    ("about 7 days to respond", "window: 7.13 days", True),
    ("under 8 days", "window: 7.13 days", True),
    ("due November 4", "deadline (2026-11-04T17:00:00+03:00)", True),
    ("$1,200 each", "1200 EA", True),
    ("a 50-page solicitation", "RFQ.pdf (50 pages)", True),
    ("nine hospitals", "", True),  # numbers spelled out are left to the verifier
])
def test_numbers_supported(text, hay, ok):
    assert rate.numbers_supported(text, hay) is ok


class StuckBatchClient:
    """A batch that never ends on its own (2026-10-24: a 35-request verify
    batch sat 5 hours). After cancel it ends with the first `done` requests
    succeeded and the rest canceled. Regular requests answer at once."""
    def __init__(self, done):
        self.done, self.canceled, self.direct = done, False, []
        self.messages = SimpleNamespace(
            create=self.create_direct,
            batches=SimpleNamespace(create=self.create, retrieve=self.retrieve,
                                    results=self.results, cancel=self.cancel))

    def create(self, requests):
        self.reqs = requests
        return SimpleNamespace(id="stuck", processing_status="in_progress")

    def retrieve(self, bid):
        return SimpleNamespace(id=bid, processing_status="ended" if self.canceled else "in_progress")

    def cancel(self, bid):
        self.canceled = True

    def results(self, bid):
        return [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
                    type="succeeded", message=msg({"ok": r["custom_id"]})) if i < self.done
                    else SimpleNamespace(type="canceled"))
                for i, r in enumerate(self.reqs)]

    def create_direct(self, **p):
        self.direct.append(p["messages"][0]["content"])
        return msg({"ok": "direct"})


def _stuck_reqs(n):
    return {f"r{i}": {"model": "claude-sonnet-5-5", "max_tokens": 10,
                      "messages": [{"role": "user", "content": f"q{i}"}]} for i in range(n)}


def test_timed_out_batch_keeps_finished_and_sends_the_rest_directly(monkeypatch):
    from baffling import batch
    monkeypatch.setattr(batch, "TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(batch, "POLL_SECONDS", 0)
    c, budget = StuckBatchClient(done=2), batch.Budget(cap=3.0)
    out = batch.run(c, _stuck_reqs(5), "verify", budget)
    assert c.canceled
    assert [out[f"r{i}"][0] for i in range(5)] == [{"ok": "r0"}, {"ok": "r1"}] + [{"ok": "direct"}] * 3
    assert sorted(c.direct) == ["q2", "q3", "q4"]
    # 2 at batch price + 3 at full price (1000 in / 100 out tokens each, Sonnet $2/$10 per M)
    one = (1000 * 2.00 + 100 * 10.00) / 1e6
    assert budget.spent == pytest.approx(2 * one * 0.5 + 3 * one)


def test_timed_out_requests_the_cap_cannot_cover_stay_unrated_and_are_not_retried(monkeypatch):
    from baffling import batch
    monkeypatch.setattr(batch, "TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(batch, "POLL_SECONDS", 0)
    c, budget = StuckBatchClient(done=1), batch.Budget(cap=0.0)
    out = batch.run(c, _stuck_reqs(3), "verify", budget)
    assert out["r0"][0] == {"ok": "r0"}
    assert [out[k][:2] for k in ("r1", "r2")] == [(None, "daily budget")] * 2
    assert c.direct == []

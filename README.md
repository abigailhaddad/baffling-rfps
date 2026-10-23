# Baffling RFP of the Week

https://baffling-rfps.pages.dev

Every day, every new federal solicitation, request for quotes, request for information (sources sought) and
presolicitation posted to SAM.gov is read by Claude. Every week, the single most baffling one gets a page, and every
other posting that looked wrong is listed.

A posting is flagged when a capable company could not tell what is being bought, the documents contradict themselves,
they contain text from a different purchase or leftover drafting notes, they ask for something no new bidder could do,
or they give too little time to respond for what is being bought.

## How it works

1. `baffling.sam` reads SAM.gov's public daily Contract Opportunities extract and finds the solicitations we have not
   seen before (`data/seen.csv`). Days to respond are counted from the solicitation's first posting.
2. `baffling.rate`: Claude Haiku pre-screens every new posting's text; Claude Sonnet reads the ones it rates 2+ (pass 1),
   then rereads the ones rated 2 or 3 with their attachments (pass 2; `baffling.attachments`: PDF and .docx).
   All calls go through the Message Batches API (`baffling.batch`, half price) under a daily cost cap
   (`BAFFLING_DAILY_CAP_USD`, default $3): what does not fit is recorded as not rated, never guessed.
3. Checks, all fail-closed (anything that fails is left out, with the reason kept in the data):
   - every quote is found in code in the file it names, with its page number
   - a contradiction needs both sides quoted
   - every number in an explanation must appear in its quotes or the posting facts
   - a second, independent call on Claude Opus checks every explanation and headline against the full documents
4. `baffling.daily` writes `data/days/<day>.json`.
5. `baffling.issue` (once a week) re-checks the finalists live on SAM.gov (cancelled, amended), has Claude pick one and
   write its headline, verifies that too, and writes `data/issues/<date>.json`.
6. `baffling.site` builds the static site; `.github/workflows/daily.yml` runs it all at 11:00 UTC and deploys to
   Cloudflare Pages.

## Limits

- Spreadsheets, old .doc files, zip files and links to other sites are not read.
- A posting that leaves SAM.gov's daily public file before it is read is missed, and a solicitation whose earlier
  versions left the file before this project started can look new.
- "Baffling" is Claude's judgment about the documents as posted.

## Running it

```
uv sync
uv run pytest
ANTHROPIC_API_KEY=... uv run python -m baffling.daily     # needs pdftotext (poppler)
ANTHROPIC_API_KEY=... uv run python -m baffling.issue
uv run python -m baffling.site --out site/dist
```

Data: SAM.gov Contract Opportunities (public domain). Code: MIT.

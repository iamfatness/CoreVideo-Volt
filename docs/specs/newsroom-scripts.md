# Newsroom scripts and workflow

What is built, as shipped. The design and the reasons are in [the design](../superpowers/specs/2026-11-01-newsroom-scripts-design.md); the build order is in [the plan](../superpowers/plans/2026-11-01-newsroom-scripts.md). Code: `volt/story.py` (markup, revisions, status, floating, prompter feed, search and copy), `volt/wire.py`, `volt/templates.py`, and the Scripts screen and prompter page in `volt/session/operator/`.

Nothing here puts text or media on air by itself. Markup never creates or fires a cue. Approval can only refuse a take, never cause one. Wire copy never reaches a rundown without a person pressing Take.

## What this has not been checked against (unvalidated)

Everything below was built and tested with fakes and fixture files. None of it has been run against:

- a real newsroom system (ENPS, iNEWS, Dalet or Octopus), or their interchange formats; the MOS story body is the only outward link and it is as unvalidated as the rest of [mos.md](mos.md);
- a real agency feed: the RSS, Atom, NewsML-style and JSON samples in `control/tests/golden/wire/` were written from general knowledge of those formats, and the NewsML reader handles a small subset only;
- a physical prompter or its software: the prompter page is a browser page and a plain-text export, nothing more;
- a real anchor: the default read time of 160 words a minute is a placeholder the customer sets per show, not a measurement;
- languages written without spaces: words are whitespace-separated tokens, so counts for such languages are wrong, and a figure such as "$1.2 billion" counts two words;
- many simultaneous editors: concurrent saves are handled by an optimistic revision check only, with no presence, locks or live co-editing;
- search quality at archive scale: search is a case-insensitive text match, adequate for thousands of stories and not a full-text index.

Whether a wire story may be stored, edited or aired is the customer's licence question; Volt only records where copy came from.

## Script markup

Plain text. A blank line starts a new paragraph. Two conventions and nothing else:

- A line that starts with `[` and ends with `]` is an **instruction** for the crew (`[SOT 0:23]`). It is shown dimmed, is not read aloud, is not counted, and does nothing on air. A line that starts with `[` and never closes is read text and is reported once as `unclosed_bracket`.
- A line that starts with `//` is a **private note**. It is not counted and never leaves the editing screens: not the prompter feed, not the text export, not the MOS story body, not a search snippet, and a search for words that sit only in a note finds nothing.

Read time is `round(words / wpm * 60)` seconds. The pace is per show (`POST /api/shows/{show}/reading-pace`, 60 to 400, `null` for the default 160). The Python (`story.script_stats`) and the page (`scriptStats`) read one golden fixture, so they cannot drift. An item with media keeps its media length; one without uses its read time (`lengthSource`: `media`, `read`, `none`).

## Revisions

Every change to a slug or script bumps `script_rev` (from 1) and stores the new text in `story_rev` (the newest 50 are kept, and the revision an approval points at is never pruned). A save quotes the revision it started from (`baseRev`): a stale base is `409 stale_script` with `current: {rev, slug, script, actor, at}` and nothing is written; saving identical text is a no-op at the same revision. The Scripts screen keeps unsaved text per story, shows both versions when a save loses the race, and never discards the editor's text without a choice (Keep mine, Take theirs, Decide later). Editing is refused for aired, skipped and killed items (`not_editable`) and for a killed story (`story_killed`); editing a story that is on air is allowed and drops its approval.

## Story status and approval

Status is `draft`, `ready`, `approved` or `killed`, separate from the playout status (a killed story is "Story killed", a playout kill is "Killed from air"; neither changes the other). Moves: draft to ready, ready to draft, ready to approved, approved to ready, any to killed, killed to draft. Anything else is `409 bad_transition`. Approving, or taking an approval back, needs `story.can_approve`: any operator or admin, or only the names in `VOLT_APPROVERS` (comma-separated; the shared legacy token is then refused). The record is who, when and which revision; "who approved revision 7" is one query on `story_event`. Changing the text of an approved story sets it back to ready. Copies, templates and recurring clones start as draft, revision 1, with no approval.

With `VOLT_REQUIRE_APPROVAL=1` (off by default) Go, Take, the clock's scheduled take and the cue path refuse a story that is not approved at its current revision (`409 story_not_approved`); a refused scheduled take is marked failed, not retried. The same setting limits the prompter to approved stories, so editing an approved story that is on air removes it from the prompter until it is approved again.

## Floating

A floated item is a backup: it stays `ready`, is passed over by Go without being marked skipped, is left out of the timing, the prompter and the MOS export, and is shown under "Floated". Only a ready item can be floated (`not_floatable`); un-floating puts it back where it was.

## Prompter

`/prompter?show=<id>&ticket=<ticket>` is a static page (large text, mirror, size and speed kept in `localStorage`, scroll holds still until started, jump to current). It polls `GET /api/shows/{show}/prompter` every 2 seconds: the airable stories in rundown order (not killed, not floated, not aired or skipped, and approved when the gate is on) as blocks of read text and instructions, notes removed on the server, with a content hash `version` so the page repaints only when what it shows has changed. The producer makes the link on the Scripts screen ("Make a prompter link", `POST /api/shows/{show}/prompter-ticket`, 4 hours by default, 12 at most). The ticket is an HMAC bound to one show and read-only; it is accepted on that feed route only and by no other route or method. It rides in the address, so it can reach logs: it is short-lived. It needs a signing key (`VOLT_MEDIA_KEY` or `VOLT_TOKEN`). `GET /api/shows/{show}/script?format=text` is the same read text as a plain-text file for print.

## Wire intake

Feeds come from the server's environment only. `VOLT_WIRE_FEEDS` is a list of `name=kind@url` separated by whitespace or semicolons (kinds `rss`, `atom`, `newsml`, `json`); a JSON feed's field mapping (`items`, `externalId`, `headline`, `body`, `byline`, `publishedAt`, dotted paths) is read from the file `VOLT_WIRE_MAPPINGS` names, keyed by feed name. Nothing set means nothing is fetched and the runner does not register the job. The runner fetches each feed that is due (every minute; five minutes after a failure) with a 10 second timeout, a 2 MB cap and http or https only, and upserts into the inbox on `(feed, external_id)`: a story still `new` is updated, a taken or dismissed one is left alone. XML with a DOCTYPE or ENTITY is refused before it is parsed, HTML bodies are reduced to text, and a failing feed records its last error (never its address) on `wire_feed` without stopping the others. `GET /api/wire` lists the inbox and each feed's state; `POST /api/wire/{id}/take` (`{show, slug?, position?}`) makes a draft story whose first line is an instruction naming the source, in one transaction with a conditional update, so two people taking at once make one item (`409 already_taken`); `POST /api/wire/{id}/dismiss` removes an entry. A wire line that starts with `//` is kept readable.

## Templates

A story template is a slug and a script skeleton (`{date}` in a slug becomes today's date); a show template is a list of story skeletons with their cues, the slot length and the pace, with no media spans and no approvals. They are made from a story or a show, or from a body, and are used with `POST /api/templates/{id}/instantiate`: a story template adds a draft to a show, a show template makes a new show. A cue the server no longer accepts is left out and counted. Deleting a template is admin only and does not touch what was made from it. Private notes stay in a template for the people who edit it.

## Archive, search and copy

Deleting a show (which is only possible when nothing aired) first copies every story into `story_archive`, in the same transaction: if the copy fails the delete fails. `GET /api/stories?q=&status=&show=&from=&to=&limit=&cursor=` searches live stories and archived ones together, newest first, each row marked `archived`; `q` matches the slug and the read text, case-insensitively, with `%` and `_` taken literally. `POST /api/items/{id}/copy` (`{show, position?}`; `archive:<id>` for an archived story) makes a draft, revision 1, with no approval. A copy is not a take: it works on a held show.

## Screens

The Scripts screen is in the Producer and Operator rails (seventh in the key order, so keys 1 to 6 are unchanged). Ctrl+S saves, from inside the script box only. Left: the stories in rundown order with their chips, a status filter and the reading pace; centre: slug, script, a preview of what is read and the count (a polite status region that changes at most once a second); right: a tabbed panel (History, Wire, Templates, Archive). See [operator-ui.md](operator-ui.md).

## Schema

Migration 25 adds the story columns on `item`, `show.read_wpm` and the tables `story_rev`, `story_event`, `wire_story`, `story_template` and `story_archive`; migration 31 adds `wire_feed`. All are mirrored in `store.SCHEMA` and `schema.sql`. Existing rows need no backfill: a NULL reads as today's behaviour.

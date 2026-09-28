# Scheduled Blog Publishing

Authors can publish a post immediately, keep it as a private draft, or schedule
it to go live automatically at a future date and time.

```
  Draft  ──(Schedule Post)──▶  Scheduled  ──(scheduled_at reached)──▶  Published
    │                              │                                      ▲
    │                              └──────(Publish Now)───────────────────┤
    └──────────────────────(Publish Now)──────────────────────────────────┘

  Scheduled ──(Save as Draft)──▶ Draft        (cancel a schedule)
  Scheduled ──(Schedule Post)──▶ Scheduled    (reschedule)
  Published ──▶ Draft/Scheduled               not allowed (409); delete the post instead
```

The feature is additive. Existing clients that send only `title` and `content`
behave exactly as before, and every post that existed before the feature is
`published`.

---

## Contents

1. [Publishing statuses](#1-publishing-statuses)
2. [Publishing options](#2-publishing-options-publish-now--save-as-draft--schedule-post)
3. [Create post API](#3-create-post-api--post-posts)
4. [Update post API](#4-update-post-api--put-postspost_id)
5. [`scheduled_at` format](#5-scheduled_at-format)
6. [`published_at` behavior](#6-published_at-behavior)
7. [Who can see what](#7-who-can-see-what)
8. [Automatic publishing mechanism](#8-automatic-publishing-mechanism)
9. [Scheduler setup](#9-scheduler--background-process-setup)
10. [Validation errors](#10-validation-errors)
11. [API examples](#11-api-examples)
12. [UI workflow](#12-ui-workflow)
13. [Local testing](#13-local-testing)
14. [Database changes](#14-database-changes)
15. [Files](#15-files)
16. [Design notes and limitations](#16-design-notes-and-limitations)

---

## 1. Publishing statuses

Every post has a `status`:

| Status | Meaning | `scheduled_at` | `published_at` | Who can see it |
|---|---|---|---|---|
| `draft` | Work in progress, never public | `null` | `null` | Author only |
| `scheduled` | Goes live automatically at `scheduled_at` | Future time | `null` | Author only, until `scheduled_at` |
| `published` | Live | Original schedule time, or `null` if never scheduled | When it went live | Everyone |

`status` is controlled by the server. Clients choose a **publishing option**
instead (see below), and the server derives the status from it.

## 2. Publishing options (Publish Now / Save as Draft / Schedule Post)

Create and update requests take an optional `publish_option`:

| UI label | `publish_option` | Resulting status | Requires |
|---|---|---|---|
| **Publish Now** | `publish_now` | `published` | Must not send `scheduled_at` |
| **Save as Draft** | `save_draft` | `draft` | Must not send `scheduled_at` |
| **Schedule Post** | `schedule` | `scheduled` | `scheduled_at`: timezone-aware and in the future |

## 3. Create post API: `POST /posts`

Requires a bearer token.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `title` | string | yes | 1–255 characters, not blank (unchanged) |
| `content` | string | yes | Not blank (unchanged) |
| `publish_option` | `"publish_now"` \| `"save_draft"` \| `"schedule"` | no | Default `"publish_now"`, the pre-feature behavior |
| `scheduled_at` | ISO 8601 datetime with timezone | only with `"schedule"` | See [section 5](#5-scheduled_at-format) |

`status` and `published_at` cannot be sent. Doing so returns `422` (an explicit
`null` is accepted and ignored).

**What each option stores**

| Option | `status` | `scheduled_at` | `published_at` |
|---|---|---|---|
| `publish_now` | `published` | `null` | Current database time |
| `save_draft` | `draft` | `null` | `null` |
| `schedule` | `scheduled` | The requested time, stored in UTC | `null` |

**Responses**

- `201`: the created post. The response has every existing field (`id`, `title`,
  `content`, `author_id`, `created_at`, `image`, `images`) plus `status`,
  `scheduled_at` and `published_at`.
- `401`: missing or invalid token.
- `403`: the plan's post limit has been reached. Drafts and scheduled posts
  count toward the limit, because they are posts the author owns.
- `422`: validation error (see [section 10](#10-validation-errors)). Nothing is created.

Image upload is unchanged. Call `POST /posts/{id}/image` after creating a post,
whatever its status.

## 4. Update post API: `PUT /posts/{post_id}`

Requires a bearer token. Only the post's author may update it.

**Request body**: `title`, `content`, `publish_option` and `scheduled_at` are
all optional.

- **Without `publish_option`**, the status and publishing times are left untouched.
  Title/content-only edits behave exactly as before, for any status.
- **With `publish_option`**, the post moves to the chosen state if the transition is allowed:

| Current status ↓ / option → | `publish_now` | `save_draft` | `schedule` |
|---|---|---|---|
| `draft` | → `published` | stays `draft` | → `scheduled` |
| `scheduled` | → `published` (schedule cleared) | → `draft` (schedule cancelled) | **reschedule** to the new time |
| `published` | no-op (original `published_at` kept) | **409** | **409** |

On each transition:

- **Publish Now**: `status=published`, `published_at` = current time, `scheduled_at=null`.
- **Save as Draft**: `status=draft`, `scheduled_at=null`, `published_at=null`.
- **Schedule**: `status=scheduled`, `scheduled_at` = new time, `published_at=null`.

**Order of checks**

1. `401`: not logged in.
2. `404`: the post doesn't exist.
3. `403`: `Not authorized to modify this post`.
4. `409`: `This post is already published and can't be moved back to draft or scheduled.`

A rejected request (`409` or `422`) changes nothing, including any `title`/`content` in the same request.

**Concurrency.** When a `publish_option` is sent, the post row is locked
(`SELECT … FOR UPDATE` on PostgreSQL) before the transition is checked. If the
scheduler is publishing the same post at that moment, the edit waits for it and
then receives the `409`. The edit can never silently take a live post back to
draft. The same lock makes the scheduler wait for an in-progress edit, after
which it re-checks the post and skips it if it's no longer `scheduled`.

## 5. `scheduled_at` format

- **Format**: ISO 8601 date **and** time **with a timezone**, for example:
  - `2026-10-01T09:00:00Z`
  - `2026-10-01T14:30:00+05:30`
  - `2026-10-01T04:00:00-05:00`
- **Rejected**:
  - a missing timezone (`2026-10-01T09:00:00`);
  - a date only (`2026-10-01`);
  - anything unparseable (`"next tuesday"`, `2026-02-30T…`, `""`).
- **Must be in the future**, compared as an absolute instant against the
  server's current UTC time. A time equal to "now" is rejected.
- **Stored in UTC.** The input offset is converted, so the same moment is kept:
  `2026-10-01T14:30:00+05:30` is stored as `2026-10-01T09:00:00Z`.
- **Responses** are always timezone-aware. PostgreSQL returns them in the
  database session's timezone (for example `+05:30`), SQLite as `Z`. Either way
  it's the same instant.

The project stores all timestamps as timezone-aware values in UTC
(`timestamp with time zone` columns, `datetime.now(timezone.utc)` in code, and
Django's `TIME_ZONE='UTC'`, `USE_TZ=True`). All scheduling comparisons follow
that convention.

## 6. `published_at` behavior

- **Set exactly once**, when the post actually goes live:
  - created with Publish Now: the insert time (database clock);
  - Publish Now on a draft or scheduled post: the time of that update;
  - published by the scheduler: the time of the scheduler run that published
    it, which is at or shortly after `scheduled_at` (within one polling interval).
- **Never changed afterwards.** Later edits, repeated Publish Now requests and
  later scheduler runs leave it alone.
- `null` for drafts and scheduled posts.
- Posts that existed before the feature were backfilled with
  `published_at = created_at`.
- `scheduled_at` is **kept** on posts the scheduler publishes, as a record of the
  planned time. It is cleared when an author publishes early or cancels.
- It cannot be set by clients (`422 published_at_read_only`).

## 7. Who can see what

The rule lives in one place, `app/services/post_publishing.py`
(`publicly_visible_clause` / `is_publicly_visible` / `can_view`):

- **published**: visible to everyone;
- **scheduled**: visible once `scheduled_at` has passed (even if the scheduler
  hasn't flipped the status yet), hidden before;
- **draft**: never public.

| Endpoint | Behavior for drafts and not-yet-due scheduled posts |
|---|---|
| `GET /posts` (feed, search, pagination) | Excluded before counting, so `total`/`total_pages` count only visible posts |
| `GET /posts/{id}` | `404 Post not found` (same as a missing post) for anyone except the author. The author sees it when sending their token. Author previews don't increase `view_count` |
| `GET /posts/{id}/comments`, `POST /posts/{id}/comments`, `POST /posts/{id}/like` | `404` for anyone except the author |
| `GET /posts/mine` | Unchanged: returns all of the author's posts, any status |
| `PUT` / `DELETE` / image upload | Unchanged: author only; others get `403` |

On `GET /posts/{id}`, an invalid or expired token is treated as anonymous
rather than `401`, so a stale browser token never breaks public reading.

The dashboard (`GET /dashboard/me`) counts all of the author's posts, including
drafts and scheduled ones, as it did before.

## 8. Automatic publishing mechanism

The project had no task queue or scheduler (no Celery/Beat, APScheduler, cron or
Django management commands), so the feature uses the simplest reliable
approach, with no new dependencies.

**The job**: `publish_due_posts()` in `app/services/post_publishing.py` runs a
single conditional statement:

```sql
UPDATE posts
   SET status = 'published', published_at = :now
 WHERE status = 'scheduled'
   AND scheduled_at IS NOT NULL
   AND scheduled_at <= :now          -- :now is the current UTC time
RETURNING id
```

- **Published only once.** Rows stop matching the moment they're published, so
  a repeated or concurrent run finds nothing left to change.
- **Scope.** Drafts, future scheduled posts and already-published posts never
  match. Any number of due posts, including many at the same instant, are
  published in one pass with one timestamp.

**One pass**: `run_once()` in `app/services/scheduled_publishing.py`:

- Opens its own session and transaction and commits.
- On any database error it rolls back, logs, and returns nothing; the next pass
  retries. A failure mid-pass leaves no half-published rows.
- On PostgreSQL it first takes a transaction-level advisory lock
  (`pg_try_advisory_xact_lock`). An overlapping pass (another worker, or the
  loop plus a cron run) skips instead of queueing. The lock is released
  automatically on commit, rollback or a lost connection.

**Restart safety.** All state lives in the `posts` table. The loop runs one pass
immediately at startup, so posts that fell due while the app was down are
published as soon as it's back.

## 9. Scheduler / background process setup

### Option A: built-in loop (default)

The FastAPI lifespan (`app/main.py`) starts a background task that calls
`run_once()` every N seconds and stops it cleanly on shutdown. Nothing extra
needs to be run; `uvicorn app.main:app` is enough.

Settings (in `.env` or the environment):

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULED_PUBLISHING_ENABLED` | `true` | `false` disables the in-app loop |
| `SCHEDULED_PUBLISHING_INTERVAL_SECONDS` | `60` | Seconds between passes (minimum 5). A post goes live at most this long after its `scheduled_at` |

The loop is started at most once per process. With several uvicorn workers,
each runs its own loop; the advisory lock keeps that safe. You may still prefer
to enable it on one worker only.

### Option B: external scheduler (cron / Windows Task Scheduler)

Run a single pass and exit:

```bash
# from the blog_api/ directory
python -m app.services.scheduled_publishing
# -> Published 2 scheduled post(s): 41, 42
```

Set `SCHEDULED_PUBLISHING_ENABLED=false` on the app when using this.

**cron** (every minute):

```cron
* * * * * cd /path/to/blog_api && .venv/bin/python -m app.services.scheduled_publishing >> publishing.log 2>&1
```

**Windows Task Scheduler** (every minute). Create a Basic Task with these settings:

| Setting | Value |
|---|---|
| Trigger | Daily, then *Repeat task every 1 minute* for *Indefinitely* |
| Program/script | `C:\Blog Management API\blog_api\.venv\Scripts\python.exe` |
| Add arguments | `-m app.services.scheduled_publishing` |
| Start in | `C:\Blog Management API\blog_api` (required so `app` can be imported and `.env` is found) |

or, from **Command Prompt** (not PowerShell):

```bat
schtasks /Create /TN "BlogScheduledPublishing" /SC MINUTE /MO 1 /TR "cmd /c cd /d \"C:\Blog Management API\blog_api\" && .venv\Scripts\python.exe -m app.services.scheduled_publishing"
```

Running both options at once is also safe: no post can be published twice.

**Logging.** Passes log at INFO (`Scheduled publishing: published N post(s): [...]`)
and database errors at ERROR. Under uvicorn's default logging only warnings and
errors from application loggers are shown, the same as the existing email
notification logs.

## 10. Validation errors

All are `422` responses in FastAPI's standard shape. `msg` is the friendly text
below, with no `Value error, ` prefix, and `type` is a stable code clients can
branch on:

```json
{
  "detail": [
    {
      "type": "scheduled_at_not_in_future",
      "loc": ["body", "scheduled_at"],
      "msg": "scheduled_at must be in the future. Choose a later date and time.",
      "input": "2020-01-01T00:00:00Z"
    }
  ]
}
```

| `type` | When | `msg` |
|---|---|---|
| `scheduled_at_required` | `schedule` without `scheduled_at` (or `null`) | scheduled_at is required when publish_option is 'schedule'. Choose a future date and time, e.g. 2026-10-01T09:00:00Z. |
| `scheduled_at_not_in_future` | Time is in the past or equal to now | scheduled_at must be in the future. Choose a later date and time. |
| `scheduled_at_missing_timezone` | No timezone, or date only | scheduled_at must include a date, a time and a timezone, e.g. 2026-10-01T09:00:00Z or 2026-10-01T14:30:00+05:30. |
| `scheduled_at_invalid_format` | Unparseable value | scheduled_at must be a valid date and time, e.g. 2026-10-01T09:00:00Z. |
| `scheduled_at_not_allowed` | `scheduled_at` sent with `publish_now`, `save_draft`, the default option, or (on update) no option | scheduled_at can only be set when publish_option is 'schedule'. Remove it, or set publish_option to 'schedule'. |
| `status_read_only` | Request contains `status` | status can't be set directly. Use publish_option instead: 'publish_now', 'save_draft' or 'schedule'. |
| `published_at_read_only` | Request contains `published_at` | published_at can't be set directly. It's recorded automatically when the post goes live. |
| `literal_error` (on `publish_option`) | Unknown option | Input should be 'publish_now', 'save_draft' or 'schedule' |

Other responses:

- `409` on update: published → draft/scheduled (see [section 4](#4-update-post-api--put-postspost_id)).
- Title/content validation errors are unchanged.

## 11. API examples

The server runs on `http://127.0.0.1:8000` (`uvicorn app.main:app --reload`).
Get a token first:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "Password123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

**Publish Now** (the same as omitting `publish_option`):

```bash
curl -X POST http://127.0.0.1:8000/posts -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Hello world", "content": "Live right away.", "publish_option": "publish_now"}'
```
```json
{"id": 57, "title": "Hello world", "content": "Live right away.", "author_id": 3,
 "created_at": "2026-09-28T11:31:36.993837+05:30", "image": null, "images": [],
 "status": "published", "scheduled_at": null, "published_at": "2026-09-28T11:31:36.993837+05:30"}
```

**Save as Draft**:

```bash
curl -X POST http://127.0.0.1:8000/posts -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Work in progress", "content": "Not ready yet.", "publish_option": "save_draft"}'
```
```json
{"id": 58, "status": "draft", "scheduled_at": null, "published_at": null, "...": "..."}
```

**Schedule Post**:

```bash
curl -X POST http://127.0.0.1:8000/posts -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Launch day", "content": "Goes live Thursday.", "publish_option": "schedule",
       "scheduled_at": "2026-10-01T14:30:00+05:30"}'
```
```json
{"id": 59, "status": "scheduled", "scheduled_at": "2026-10-01T14:30:00+05:30", "published_at": null, "...": "..."}
```

**Draft → Scheduled**:

```bash
curl -X PUT http://127.0.0.1:8000/posts/58 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"publish_option": "schedule", "scheduled_at": "2026-10-02T09:00:00Z"}'
```

**Reschedule** (a scheduled post, new time):

```bash
curl -X PUT http://127.0.0.1:8000/posts/59 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"publish_option": "schedule", "scheduled_at": "2026-10-05T09:00:00Z"}'
```

**Cancel a schedule** (scheduled → draft):

```bash
curl -X PUT http://127.0.0.1:8000/posts/59 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"publish_option": "save_draft"}'
```

**Publish a draft or scheduled post now**:

```bash
curl -X PUT http://127.0.0.1:8000/posts/58 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"publish_option": "publish_now"}'
```

**Edit text only** (status untouched):

```bash
curl -X PUT http://127.0.0.1:8000/posts/59 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Launch day (updated)"}'
```

**Author previews their own draft** (others get `404`):

```bash
curl http://127.0.0.1:8000/posts/58 -H "Authorization: Bearer $TOKEN"
```

**List own posts with their statuses**:

```bash
curl http://127.0.0.1:8000/posts/mine -H "Authorization: Bearer $TOKEN"
```

**PowerShell equivalent** (Schedule Post):

```powershell
$body = @{ title = "Launch day"; content = "Goes live Thursday."; publish_option = "schedule";
           scheduled_at = "2026-10-01T14:30:00+05:30" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/posts -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $TOKEN" } -Body $body
```

**Rejected: time in the past**:

```bash
curl -X POST http://127.0.0.1:8000/posts -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "t", "content": "c", "publish_option": "schedule", "scheduled_at": "2020-01-01T00:00:00Z"}'
# 422 {"detail":[{"type":"scheduled_at_not_in_future","loc":["body","scheduled_at"],
#       "msg":"scheduled_at must be in the future. Choose a later date and time.", ...}]}
```

## 12. UI workflow

The web app (`/static/dashboard.html`) keeps its existing layout. Two areas changed.

### Write / Edit page: Publishing options

Below the story text there is a **Publishing options** section with three choices:

- **Publish Now** (default): the button reads *Publish story*. After saving, the
  story is live and you're taken to the feed.
- **Save as Draft**: the button reads *Save draft*. After saving: *"Draft saved.
  Only you can see it."*, then **My posts**.
- **Schedule Post**: shows a **Date** picker and a **Time** picker, pre-filled
  with tomorrow on the hour, plus a note of your local timezone (e.g.
  *Asia/Kolkata*). The button reads *Schedule story*. After saving: *"Scheduled
  for Wed, 30 Sept, 9:30 am"*, then **My posts**.

The date and time fields are hidden for Publish Now and Save as Draft.

**Scheduling validation (in the browser)**

- The date picker doesn't allow past days. For today, the time picker doesn't allow past times.
- Before anything is sent, one of these appears under the pickers and the fields are highlighted:
  - *Choose a date and time to schedule your story.* (or *Choose a date…* / *Choose a time…*)
  - *That time is in the past. Choose a future date and time.*
  - *Choose a time at least a minute from now.* (a small buffer so the time is still in the future when it reaches the server)
- The chosen local date and time is sent to the API converted to UTC.
- Errors returned by the API are shown in plain language, e.g. *The scheduled
  time must be in the future. Choose a later date and time.*

**Editing an existing post**

| Post is… | Editor opens with… | Notes |
|---|---|---|
| Draft | *Save as Draft* selected | Switch to Publish Now or Schedule Post to change it |
| Scheduled | *Schedule Post* selected, with its date and time filled in | Change the time to reschedule, pick Save as Draft to cancel, or Publish Now to go live early |
| Published | *Publish Now* selected; Draft and Schedule are disabled | *"This story is already live. To take it down, delete it from My posts."* |

The button reads *Save changes* when the option matches the current status, and
otherwise names the action (*Publish now*, *Save draft*, *Update schedule*,
*Schedule story*). Cover image upload works the same for every option.

### My posts: publishing status

Each post shows a status badge and when it goes, or went, live:

| Badge | Line |
|---|---|
| **Draft** (grey) | Only visible to you |
| **Scheduled** (orange) | Scheduled for: Thu, 1 Oct, 2026, 6:45 pm |
| **Published** (green) | Published: Mon, 28 Sept, 2026, 12:51 pm |

Times are shown in the viewer's local timezone. A scheduled post whose time has
just passed shows *"· going live now"* until the next scheduler pass publishes
it. **View**, **Edit** and **Delete** are unchanged, and View opens your own
drafts. The public feed never shows drafts or not-yet-due scheduled posts.

### Example workflow in the UI

1. **Write** → enter title and text → **Save as Draft** → *Save draft*. The post
   appears in My posts as **Draft**.
2. My posts → **Edit** → **Schedule Post** → pick a date and time →
   *Schedule story*. It now shows **Scheduled** with *Scheduled for: …*.
3. At that time the scheduler publishes it. After a refresh, My posts shows
   **Published** with *Published: …*, and the story appears in the public feed.

## 13. Local testing

### Apply the database migration

```bash
cd blog_api
alembic current          # expect d93b7e5c1a46 (before) or f4a91c7e2b58 (after)
alembic upgrade head     # applies f4a91c7e2b58_add_post_publishing_status
```

The migration is idempotent: it inspects before adding. App startup also adds
the columns to older SQLite databases (`ensure_post_publishing_columns()` in
`app/database.py`). **Do not** use `alembic revision --autogenerate` against the
shared database; it also sees Django's tables.

### Run the app and watch a post go live

```bash
# publish checks every 5 s instead of 60 s, and don't send real emails while testing
SCHEDULED_PUBLISHING_INTERVAL_SECONDS=5 EMAIL_ENABLED=false uvicorn app.main:app --reload
```

PowerShell:

```powershell
$env:SCHEDULED_PUBLISHING_INTERVAL_SECONDS = "5"; $env:EMAIL_ENABLED = "false"; uvicorn app.main:app --reload
```

Then either:

- open `http://127.0.0.1:8000/static/dashboard.html`, schedule a post 1–2 minutes
  ahead, and refresh **My posts** after that time; or
- schedule one via the API (section 11) and poll `GET /posts/{id}` with your
  token until `status` is `published`.

To run a single pass by hand: `python -m app.services.scheduled_publishing`.

### Automated tests

```bash
# everything (about 10 minutes; uses isolated in-memory SQLite databases)
python -m pytest -q

# just this feature
python -m pytest -q tests/test_scheduled_publishing_feature.py \
  tests/test_scheduled_publishing_validation_review.py tests/test_scheduled_publishing.py \
  tests/test_post_publishing_schemas.py tests/test_post_publishing_create_api.py \
  tests/test_post_publishing_update_api.py tests/test_post_visibility.py
```

| Test file | Covers |
|---|---|
| `test_scheduled_publishing_feature.py` | The feature checklist 1–22: creation, visibility, updating, automatic publishing, security |
| `test_scheduled_publishing_validation_review.py` | The 15 validation edge cases (formats, timezones, races, restarts) |
| `test_scheduled_publishing.py` | The job, `run_once`, error handling, the background loop and lifespan |
| `test_post_publishing_schemas.py` | Request/response schemas and messages |
| `test_post_publishing_create_api.py` | `POST /posts` with each option |
| `test_post_publishing_update_api.py` | Every `PUT` transition, 409s and ownership |
| `test_post_visibility.py` | Feed, search, pagination, detail, comments and likes visibility |

`tests/conftest.py` turns the background loop off for every test (otherwise it
would run against the `.env` database). Tests of the job call `run_once()`
themselves and simulate time passing by moving the service clock
(`post_publishing._utc_now`), never by writing past timestamps through the API.

## 14. Database changes

Migration `alembic/versions/f4a91c7e2b58_add_post_publishing_status.py` (revises
`d93b7e5c1a46`). It only adds to the `posts` table; nothing is renamed or removed.

| Change | Definition |
|---|---|
| `status` | `VARCHAR(20) NOT NULL DEFAULT 'published'` |
| `scheduled_at` | `TIMESTAMP WITH TIME ZONE NULL` |
| `published_at` | `TIMESTAMP WITH TIME ZONE NULL`; backfilled with `created_at` for existing posts |
| `ck_posts_status_valid` | `status IN ('draft', 'scheduled', 'published')` |
| `ck_posts_scheduled_requires_scheduled_at` | `status <> 'scheduled' OR scheduled_at IS NOT NULL` |
| `ix_posts_status_scheduled_at` | Index on `(status, scheduled_at)`, used by the scheduler query and feed filtering |

On SQLite the check constraints exist only on databases created fresh by the
app, because SQLite can't add constraints to an existing table.

## 15. Files

| File | Role |
|---|---|
| `app/models.py` | `Post.status` / `scheduled_at` / `published_at`, status constants, constraints, and `published_at` set on insert for published posts |
| `app/schemas.py` | `publish_option` / `scheduled_at` on `PostCreate` and `PostUpdate`, validation messages (`SCHEDULED_AT_ERRORS`), and the new fields on `PostResponse` |
| `app/routers/posts.py` | Create, update (transitions and row lock), feed filtering, and author-aware detail |
| `app/routers/common.py` | `get_visible_post_or_404` |
| `app/routers/comments.py`, `app/routers/likes.py` | Use the visibility rule |
| `app/auth.py` | `get_optional_current_user` for public endpoints that show more to the author |
| `app/services/post_publishing.py` | Transition rules, visibility rule, `publish_due_posts` |
| `app/services/scheduled_publishing.py` | `run_once`, background loop, advisory lock, CLI entry point |
| `app/main.py` | Starts and stops the loop in the lifespan |
| `app/database.py` | `ensure_post_publishing_columns()` for older databases |
| `app/static/dashboard.html` | Publishing options UI and My posts status display |
| `alembic/versions/f4a91c7e2b58_add_post_publishing_status.py` | Migration |

## 16. Design notes and limitations

- **No unpublishing.** Before this feature every post was public from creation
  and the only way to remove one was deleting it, so published → draft/scheduled
  returns `409`. To allow it, add `SAVE_DRAFT`/`SCHEDULE` to the `published`
  entry of `ALLOWED_PUBLISH_OPTIONS` in `app/services/post_publishing.py`.
- **Publishing delay** is at most one polling interval (60 s by default) after
  `scheduled_at`. Visibility doesn't wait for it: a due post is already public,
  and only its `status`/`published_at` update on the next pass.
- **Plan limits** count drafts and scheduled posts, and editing or
  status changes never consume extra quota.
- **No notifications** are sent when a scheduled post goes live.

# Phase 2B — Send it to Keap (delivery)

> **Prompt to start (paste into Claude Code / ChatGPT with this repo open):**
> "Implement Phase 2B exactly as described in `docs/PHASE_2B_KEAP_DELIVERY.md`. Phase 2A
> (`docs/PHASE_2A_KEAP_DATA.md`) must already be merged. Read `CLAUDE.md`, `context/PROJECT_CONTEXT.md`,
> `docs/PHASE2.md`, both phase files and `../Reference/Keap/docs/CLAUDE.md` first. Build the migration, the
> Keap OAuth + client + sync code, the keap-worker, the CLI commands, the dashboard status endpoint and every
> test in section 12, using a fake Keap (httpx.MockTransport) — never the real Keap in automated tests.
> Run the full suite and ruff, then open a PR. Use the tag and field ids agreed in Phase 2A section 0 —
> never guess an id or create a tag/field in Keap."

---

## 1. Goal in one paragraph

Phase 2A fills `keap_sync.desired` (what each person's Keap contact should look like) and queues the row.
Phase 2B is a separate background service, **keap-worker**, that logs in to Keap with OAuth, keeps the
tokens fresh, takes queued rows, finds or creates the Keap contact without making duplicates, sets the
owned fields, applies/removes the owned tags, records what it did, and retries safely within Keap's rate
limits. Keap being slow or down must never affect calls, texts, the Sheet or the Stride import.

## 2. Background you need

- Repo conventions: `CLAUDE.md` (raw SQL via `db.transaction()`, logic in `services/`, migrations
  numbered — 2A uses **036**, you use **037**, never edit an applied one, comments explain *why*).
- HTTP: extend `services/provider_http.py::ProviderService`; call `self._request(trace, "keap", method, url,
  params=..., json=..., idempotent=...)`. It retries transient errors and short `Retry-After` 429s itself,
  records PHI-free `integration_events`, and logs the **url** — so always pass query strings through
  `params=`, never inside `url` (filters contain emails/phones).
- Retry delays: `retry.py::retry_delay_seconds(attempt, retry_after_seconds, settings)`.
- Worker template: `sheet_sync_worker.py` (claim with `for update skip locked`, mark sending, deliver,
  mark delivered/retry/dead, loop with `time.sleep`) and `stride_import_worker.py` (heartbeat into
  `service_heartbeats`, plain-language dashboard status in `services/stride_sync_status.py`).
- **Do not touch** the old signed handoff (`services/keap_service.py`, `appointment.booked.v1` rows in
  `integration_outbox` with `destination='keap'`, delivered by `services/delivery.py`). It is a different,
  older contract owned by `KEAP_MODE`. The new sync uses only `keap_sync` and its own worker.
- Logging: `observability.py` redacts `Authorization`, bearer tokens, emails, phones, DOB, but **never log
  tokens or person data on purpose**. `last_error` columns hold short PHI-free reasons.
- Keap docs (scraped, offline): `../Reference/Keap/docs/` — `rest-v2/contact.md`, `rest-v2/tags.md`,
  `guides/getting-started-oauth-keys.md`, `guides/api-token-quota-and-usage-measurements.md`.
- Environments: demo Supabase (`.env`, live server today) first; client production only when the user
  says so. Keap: **sandbox app `puq284`** for all testing. Sandbox and production Keap have different
  tag/field IDs and tokens — never mix them.

## 3. Keap REST v2 — exactly what to call

Base `https://api.infusionsoft.com/crm/rest/v2`, header `Authorization: Bearer <access_token>`,
`Content-Type: application/json`. IDs are **strings**.

### 3.1 OAuth (Authorization Code + Refresh Token)
| Step | Call |
| --- | --- |
| Send admin to | `GET https://accounts.infusionsoft.com/app/oauth/authorize?client_id=…&redirect_uri=…&response_type=code&scope=full&state=…` (redirect must be HTTPS and registered in the Keap developer app) |
| Exchange code | `POST https://api.infusionsoft.com/token`, `application/x-www-form-urlencoded`: `client_id`, `client_secret`, `code`, `grant_type=authorization_code`, `redirect_uri` (same as above) |
| Refresh | `POST https://api.infusionsoft.com/token`, form `grant_type=refresh_token&refresh_token=<latest>`, header `Authorization: Basic base64(client_id:client_secret)` |
| Response | `access_token`, `refresh_token`, `expires_in` (seconds), `token_type`, `scope` |

- Every refresh returns a **new refresh token**; the old one stops working. Save both in one transaction.
- Refresh when less than 3 hours remain (`expires_in` is about 24 h; do not hard-code it).
- An `invalid_grant`/400/401 on refresh = re-authorization needed (refresh tokens also expire when unused
  for a long time).

### 3.2 Contacts
| Purpose | Call | Notes |
| --- | --- | --- |
| Read one | `GET /contacts/{id}` with `params={"fields": "id,given_name,family_name,email_addresses,phone_numbers,birth_date,contact_type,custom_fields,tag_ids"}` | 404 = deleted/merged in Keap |
| Find by email | `GET /contacts` with `params={"filter": "email==<email>", "fields": "id,given_name,family_name", "page_size": 10}` | exact match; `;` joins filters |
| Find by phone | `GET /contacts` with `params={"filter": "phone_number==<digits>;phone_fields==PHONE1,PHONE2,PHONE3", "fields": …}` | `phone_fields` is required with `phone_number` |
| Create | `POST /contacts` (add `params={"duplicate_option": "EmailAndName"}` when there is an email) | 201 created, 200 = matched existing and **updated it**. Needs at least one email, phone or address; `country_code` required if `region_code` set |
| Update | `PATCH /contacts/{id}` with `params={"update_mask": "<comma list>"}` | **Arrays are replaced whole**: `email_addresses`, `phone_numbers`, `addresses`, `custom_fields` — anything not sent is deleted. Always read, merge, write |
| Custom field catalogue | `GET /contacts/model` | `custom_fields[]`: `id`, `label`, `field_type` (DATE, DATE_TIME, TEXT, …), `field_name` |

Body shapes:
```json
{"given_name":"…","family_name":"…","birth_date":"1985-03-15","contact_type":"Lead",
 "email_addresses":[{"email":"…","field":"EMAIL1"}],
 "phone_numbers":[{"number":"+15615550123","field":"PHONE1"}],
 "addresses":[{"field":"BILLING","line1":"…","locality":"…","region_code":"US-FL","postal_code":"…","country_code":"USA"}],
 "custom_fields":[{"id":"17","content":"2026-10-15"}]}
```

### 3.3 Tags
| Purpose | Call |
| --- | --- |
| List (for discovery) | `GET /tags` `params={"page_size": 1000}` (+ `page_token`); `filter=name==<name>` |
| Apply | `POST /tags/{tag_id}/contacts:applyTags` body `{"contact_ids": ["<id>"]}` → 200 |
| Remove | `POST /tags/{tag_id}/contacts:removeTags` body `{"contact_ids": ["<id>"]}` → 204 |
| Tags on a contact | `GET /contacts/{id}/tags` (or `tag_ids` in the contact read) |

### 3.4 Errors and limits
- Error body: `{"code": int, "message": str, "status": str, "details": [...]}`. Store only `code`/`status`.
- OAuth limits: **1,500 requests/minute and 150,000/day** per client app; plus **10,000/minute and
  250,000/day per Keap account**; spike limit ~25/second. Response headers:
  `x-keap-product-throttle-available`, `x-keap-product-quota-available`, `x-keap-tenant-throttle-available`.
  On 429 honour `Retry-After`.

### 3.5 Verify on day 1 in the sandbox (the docs do not say) — write the answers into this file
| Question | How |
| --- | --- |
| Value format for a DATE custom field (`"2026-10-15"`? ISO date-time?) | set one on a test contact, read it back |
| DATE_TIME format | same |
| Exact `contact_type` string Keap uses for "Lead" | read a contact created as Lead in the UI |
| `update_mask` as comma list vs repeated param | try both on a test contact |
| `applyTags` on a contact that already has the tag | must be 200, no error |
| Phone filter wants national digits or E.164 | search a known test contact |
| `duplicate_option=EmailAndName` with same email, different first name | must create a new contact |

## 4. Database — migration `037_keap_connection.sql`

```sql
-- OAuth tokens for the Keap account this practice syncs to. Backend only:
-- never exposed to the dashboard, n8n or logs. Tokens rotate on every refresh.
create table if not exists public.keap_connections (
  practice_id bigint primary key references public.practices(id) on delete restrict,
  keap_account text,                          -- e.g. 'puq284' (sandbox) — a label to avoid mixing accounts
  access_token text not null,
  refresh_token text not null,
  access_token_expires_at timestamptz not null,
  status text not null default 'active' check(status in ('active','reauthorization_required')),
  last_refreshed_at timestamptz,
  last_error text,                            -- PHI-free, never a token
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.keap_connections enable row level security;
```
The keap-worker heartbeat reuses `service_heartbeats` (migration 032).

## 5. Code to write

| File | What |
| --- | --- |
| `src/rpt_agent/services/keap_oauth.py` | `authorize_url(state)`, `exchange_code(code)`, `access_token(conn)` (refresh under `select … for update` when < 3 h left), `force_refresh(conn)` (after a 401), `mark_reauthorization_required(conn, reason)` |
| `src/rpt_agent/services/keap_client.py` | `KeapClient(ProviderService)`: `get_contact`, `find_by_email`, `find_by_phone`, `create_contact`, `update_contact(id, body, mask)`, `apply_tag`, `remove_tag`, `contact_model`, `list_tags`; a rate limiter; 401 → one `force_refresh` and one retry |
| `src/rpt_agent/services/keap_sync.py` | `sync_one(conn_factory, client, row)` — section 6 |
| `src/rpt_agent/services/keap_sync_status.py` | Dashboard status (like `stride_sync_status.py`) |
| `src/rpt_agent/keap_sync_worker.py` | `rpt-keap-worker` loop — section 7 |
| `src/rpt_agent/routes/keap.py` | OAuth start/callback — section 8 (register in `api.py`) |
| `src/rpt_agent/routes/dashboard.py` | `GET /api/v1/dashboard/keap-sync` |
| `src/rpt_agent/cli.py` | `rpt keap-discover`, `rpt keap-sync-once --lead <uuid> | --patient <stride id>`, `rpt keap-retry-failed` |
| `pyproject.toml` | `rpt-keap-worker = "rpt_agent.keap_sync_worker:main"` |
| `docker-compose.prod.yml` / `docker-compose.yml` | `keap-worker` service, `command: rpt-keap-worker`, `mem_limit: 256m`, same build/env as `sheet-worker` |
| `config.py`, `.env.example` | section 10 |

## 6. Syncing one person — `sync_one`

Input: one claimed `keap_sync` row (status `syncing`) with `desired` (shape in 2A §5). Do the steps in
order; each Keap write is safe to repeat.

1. **Resolve the contact id**
   - Have `keap_contact_id` → `get_contact`. 404 → clear the id (contact deleted or merged in Keap) and fall
     through to search.
   - No id → **search before create** (never trust Keap's email-only duplicate check: family members share
     emails):
     1. `match.email` → `find_by_email`; keep candidates whose `given_name` **and** `family_name` equal
        (case-insensitive, trimmed) `match.given_name`/`family_name`.
     2. none and `match.phone_digits` → `find_by_phone`, same name check.
     3. exactly one candidate → use it. More than one → `status='needs_review'`,
        `last_error='several Keap contacts match'`, stop.
     4. none → `create_contact` with the full contact body + all non-null owned custom fields (formatted,
        §6.1). **Immediately** save the returned id in its own committed transaction before anything else,
        so a crash cannot create a second contact.
   - A create that times out is *ambiguous*: do not retry the create; leave the row `retrying`. The next
     attempt starts with the search and finds the contact if it was made.
2. **Fields** (skip if just created with everything): from the read contact build the PATCH:
   - `given_name`, `family_name`, `birth_date`, `contact_type`: send if different.
   - `email_addresses`: if `desired.contact.email` is already in any slot → unchanged; else replace/insert
     `EMAIL1` and keep EMAIL2/EMAIL3 as they are.
   - `phone_numbers`: same rule on digits for `PHONE1`; keep PHONE2–PHONE5.
   - `custom_fields`: start from the contact's current list, drop every id in `desired.owned_fields`, add
     each owned field whose desired value is not null (formatted). Send only if the result differs.
   - `addresses`: only when desired has one and the contact has none.
   - One `update_contact` with `update_mask` = the properties you changed. Nothing changed → no call.
3. **Tags**: `have = contact.tag_ids`; `want = desired.tags`; `owned = desired.owned_tags`.
   - Apply every tag in `want - have` **first** (so a person is never left with no status tag).
   - Then remove every tag in `(owned ∩ have) - want`. Never touch tags outside `owned`.
4. **Finish** (one transaction): if `desired_hash` still equals the hash you started with →
   `status='synced', synced=desired, synced_hash=desired_hash, synced_at=now(), attempts=0,
   last_error=null`; else (2A changed it meanwhile) → `status='pending', next_attempt_at=now()`.

### 6.1 Formatting custom field values (by `field_type` from `GET /contacts/model`, cached 1 h)
| `kind` in desired | Keap `field_type` | Send |
| --- | --- | --- |
| `date` | DATE | the confirmed format from §3.5 (expected `YYYY-MM-DD`) |
| `date` + `time` for the same type | DATE_TIME | combine local date + time → confirmed format |
| `time`, `text` | TEXT / TEXT_AREA | the string as is |
| any | anything else | do not send; `status='failed'`, `last_error='unsupported field type <type> for field <id>'` |

## 7. keap-worker loop — `keap_sync_worker.py`

Every `KEAP_SYNC_POLL_SECONDS` (30):
1. Exit early (heartbeat only) if `KEAP_SYNC_ENABLED=false` or the connection is missing or
   `reauthorization_required`.
2. Take a session advisory lock (as `worker.py` does) so only one keap-worker ever runs.
3. Reset rows stuck in `syncing` for over 15 minutes to `retrying` (worker died mid-sync).
4. Phase 2A: `keap_desired.plan_changes(conn, practice_id)`; with `full=True` once an hour.
5. Ensure a valid access token (`keap_oauth.access_token`).
6. Claim up to `KEAP_SYNC_BATCH_SIZE` (20) rows: `status in ('pending','retrying') and next_attempt_at<=now()
   order by next_attempt_at, id for update skip locked`, set `status='syncing', attempts=attempts+1`.
7. `sync_one` each, respecting the rate limiter. Errors → §9.
8. Heartbeat `service_heartbeats('keap-worker')` with counts (pending, failed, needs_review, requests used
   this minute/today, connection status). Write it every tick, even when idle or disabled.

### 7.1 Rate limiter
In-process token bucket: `KEAP_MAX_REQUESTS_PER_MINUTE` (default 120 — far below Keap's 1,500) and a daily
budget `KEAP_MAX_REQUESTS_PER_DAY` (default 40,000, resets 00:00 UTC). Before every request take one token
(sleep if empty). If any `x-keap-*-available` header drops under 10% of its limit, sleep until the next
minute. A 429 with a long `Retry-After` (returned by `_request`) → stop the tick and set the rows'
`next_attempt_at` to now + `Retry-After`. A backlog (first full load) simply drains over hours/days;
2A's priority keeps new leads first.

## 8. OAuth endpoints — `routes/keap.py`

- `GET /api/v1/keap/oauth/start` — dashboard auth, **super_admin only** (`_require_super_admin` pattern in
  `routes/dashboard.py`). Returns `{"authorize_url": …}`. `state` = signed `practice_id|expiry` (reuse the
  HMAC style of `security.sign_slot`, 10-minute expiry).
- `GET /api/v1/keap/oauth/callback?code=…&state=…` — public (Keap redirects the browser here). Verify
  `state`, exchange the code, upsert `keap_connections` (status `active`), return a tiny HTML page
  "Keap connected". Never echo tokens. Caddy already proxies every path to the API.
- Register `https://stride.aibolt.ai/api/v1/keap/oauth/callback` as the redirect URI in the Keap developer
  app. For local testing, ngrok (see `docs/` local Vapi guide) or temporarily paste the `code` into
  `rpt keap-sync-once --code` — your choice, document it.
- If a colleague already has working token-refresh code, adapt it to these functions and this table.

## 9. Failure handling

| Failure | What to do |
| --- | --- |
| Keap down, 5xx, timeout on a read | row `retrying`, `next_attempt_at = now + retry_delay_seconds(attempt)`; never block other work |
| Timeout on create | ambiguous: `retrying`; next attempt searches first (§6 step 1) |
| 429 | honour `Retry-After` (§7.1) |
| 401 on a call | `force_refresh` once, retry the call once; 401 again → `mark_reauthorization_required`, stop the tick |
| Refresh rejected | `reauthorization_required`; dashboard alert "Reconnect Keap"; rows wait (no attempts used) |
| 400 on create/update (bad value) | `failed`, `last_error='<call> rejected: <code>'` |
| Unknown tag or field id (400/404 on tag/field) | `failed`, `last_error='tag <id> not found in Keap'` — mapping is wrong |
| Several possible contacts | `needs_review` |
| Attempt `KEAP_SYNC_MAX_ATTEMPTS` (3) reached | `failed`; shown on the dashboard; `rpt keap-retry-failed` (or a later dashboard button) resets to `pending` |
| Desired changes while `failed` | 2A sets `pending` again (new hash) → retried automatically |

## 10. Configuration (`config.py` + `.env.example`; secrets only in the server `.env`)

| Variable | Default |
| --- | --- |
| `KEAP_SYNC_ENABLED` | `false` |
| `KEAP_CLIENT_ID`, `KEAP_CLIENT_SECRET` | empty (required when enabled) |
| `KEAP_REDIRECT_URI` | `https://stride.aibolt.ai/api/v1/keap/oauth/callback` |
| `KEAP_API_BASE_URL` | `https://api.infusionsoft.com/crm/rest/v2` |
| `KEAP_TOKEN_URL` | `https://api.infusionsoft.com/token` |
| `KEAP_AUTHORIZE_URL` | `https://accounts.infusionsoft.com/app/oauth/authorize` |
| `KEAP_SYNC_POLL_SECONDS` | `30` |
| `KEAP_SYNC_BATCH_SIZE` | `20` |
| `KEAP_SYNC_MAX_ATTEMPTS` | `3` |
| `KEAP_MAX_REQUESTS_PER_MINUTE` | `120` |
| `KEAP_MAX_REQUESTS_PER_DAY` | `40000` |

Add `runtime_errors("keap-worker")` checks: DB URL; when enabled, client id/secret present and the
redirect URI is HTTPS in deployed environments.

## 11. Dashboard status — `GET /api/v1/dashboard/keap-sync`
Plain words, no patient data, same style as `/stride-sync`: `overall` (ok / attention / problem /
not_started / switched_off), `headline`, `connection` (connected, account label, token valid until,
reconnect needed), counts by status, `today` (contacts created, updated, tags applied/removed), the 10 latest
`failed`/`needs_review` rows as `{reason, since}` only, heartbeat age. Alerts: worker stopped (> 5 min),
reconnect needed, failed > 0, backlog > 500 pending for > 1 h, daily budget > 80 % used.

## 12. Testing

### 12.1 Fake Keap (automated tests never call the real Keap)
`tests/fake_keap.py`: a small in-memory Keap served through `httpx.MockTransport`, passed into
`KeapClient(client=httpx.Client(transport=...))`. It keeps `contacts{id: {...}}`, `tags{id: name}`,
`custom_fields{id: field_type}`, and implements exactly the calls in §3 (filters on email and phone digits,
PATCH replacing arrays like the real API, `duplicate_option=EmailAndName`, applyTags/removeTags, `/token`).
Switches to inject: next response 500 / 429 with `Retry-After` / 401 once / 401 always / timeout on create
(contact still created) / 404 for an id / 400 for an unknown tag. It counts requests per endpoint.

### 12.2 Unit + DB tests (`TEST_DATABASE_URL`, local Postgres — see 2A §9.1)
| Test | Expect |
| --- | --- |
| New lead, nothing in Keap | 1 create, id saved, Pending applied, `synced` |
| Same row synced twice | second run makes 0 writes |
| Contact already in Keap (same email + name) | no create, id reused |
| Same email, different first name (family) | new contact created |
| Email search empty, phone + name match | reused |
| Two matches | `needs_review`, no write |
| Timeout on create, then next attempt | found by search, **one** contact total |
| Crash after create before saving id is impossible | id committed before step 2 (assert ordering) |
| Booked: Pending → IE Booked | apply IE first, then remove Pending (order asserted) |
| Cancel | IE Booked removed; non-owned tags untouched |
| Custom fields | date/time/clinician set; a non-owned custom field on the contact survives the PATCH |
| Clear a date (desired null) | owned field dropped from list, others kept |
| Second email already in EMAIL2 | emails unchanged |
| Phone changed | PHONE1 updated, PHONE2 kept |
| Unsupported field type | `failed` with reason |
| 500 then success | `retrying` then `synced` |
| 3 failures | `failed` |
| 429 long Retry-After | tick stops, `next_attempt_at` respects it |
| 401 once | refresh + retry succeeds; both tokens rotated and saved together |
| 401 always / refresh rejected | `reauthorization_required`, rows untouched |
| Token refresh when < 3 h left, under row lock | one refresh even with two concurrent callers |
| Desired changed during sync | ends `pending`, not `synced` |
| Stuck `syncing` > 15 min | reset to `retrying` |
| Contact 404 (deleted in Keap) | id cleared, search, recreate/reuse |
| Rate limiter | never more than N requests per simulated minute |
| OAuth callback | bad/expired `state` rejected; good → tokens stored, never in response/logs |
| Status endpoint | PHI-free; alerts for reconnect / failed / stopped |
| Logs | no emails, phones, names, tokens in captured logs |

### 12.3 Sandbox end-to-end (manual, `puq284`) — the run I (Claude) will repeat after merge
Use only synthetic people: `555-01xx` phones, `@example.test` emails.
1. Local Postgres (2A §9.1), `KEAP_SYNC_ENABLED=true`, sandbox client id/secret in a local env (not
   committed), `KEAP_INCLUDE_TEST_LEADS=true`.
2. Authorize once (§8), then `rpt keap-discover`: prints every tag and custom field (`id`, name/label,
   `field_type`). Find the Keap admin's Pending, Booked/Completed and date/time/clinician fields; insert
   them into `keap_appointment_types` and `practice_settings.keap_pending_tag_id`; set
   `keap_sync_start_at = now() - interval '1 day'`.
   **If the admin's tags are not listed, the token belongs to a different Keap account** — check
   `keap_account` and ask which account she used.
3. Answer §3.5 with a throw-away contact.
4. Personas (create the leads in the local DB, Stride data with `scripts/fake_stride.py feed` + the local
   Stride import worker, or the CSV helpers from `tests/test_stride_import_integration.py`):

| Persona | Do | Check in Keap |
| --- | --- | --- |
| Quinn — Sheet lead | add lead | contact, Pending |
| Quinn books | Stride IE appointment in 3 days | IE Booked + date/time/clinician, Pending gone |
| Quinn checked in | status Checked In | IE Completed, Booked kept |
| Rory — lead, then cancels | book, then Cancel | Booked removed, date cleared |
| Sky — walk-in | Stride patient + Follow-up | contact, FU Booked, no Pending |
| Sam & Sue — same email | two patients | two contacts |
| Pre-existing — make a contact by hand in Keap first (same email + name, plus a custom field of the admin's own) | add lead | reused, admin's field kept |
| Phone change | new mobile in Stride | PHONE1 changed |
| Deleted in Stride | Delete row | nothing changes in Keap |

5. Run `rpt-keap-worker` locally (never against the live `.env` DB) and watch `/api/v1/dashboard/keap-sync`.
6. Clean up the sandbox test contacts afterwards (by hand or a small script listing `@example.test`).

## 13. Deploy (demo first)
1. PR merged → on the server: `git pull`, build, `docker compose -f docker-compose.prod.yml run --rm api rpt
   migrate`, `up -d` (see `../Deployment.md`). `keap-worker` starts switched off.
2. Add `KEAP_CLIENT_ID/SECRET` to the server `.env` (never in git), restart `api` and `keap-worker`.
3. A super admin opens the authorize URL (sandbox account first), approves, sees "Keap connected".
4. `rpt keap-discover`, fill the mapping + settings with SQL on **demo**.
5. `KEAP_SYNC_ENABLED=true`, restart `keap-worker`, watch the logs and `/dashboard/keap-sync`.
6. Production Keap later: same steps with the client's real Keap account and IDs, only on the user's go.

## 14. Done when
- All tests in §12.2 pass with the full suite and ruff; §3.5 answers written into this file.
- Sandbox personas in §12.3 behave as listed.
- `docs/PHASE2.md`, `context/PROJECT_CONTEXT.md` changelog and folder `CLAUDE.md` tables updated.

## 15. Open questions (default in brackets)
1. Remove Booked when cancelled? [yes] — client.
2. Exact `contact_type` value. [find in sandbox]
3. Which Keap account did the admin create the tags in? (We could not see them.) — ask the Keap admin.
4. Do they want the Pending tag ID created too? (Our last email only asked for Booked/Completed tags and
   date/time/clinician fields.) — ask the Keap admin.
5. Email opt-in (`opt_in_reason`) for marketing — not set by us. [not set]

# n8n workflows

Import these four files in order:

```text
workflows/01-lead-action-intake.workflow.json
workflows/02-lead-action-recovery.workflow.json
workflows/03-sheet-update-webhook.workflow.json
workflows/04-lead-profile-sync.workflow.json
```

Workflow 04 is separate from Action intake. It watches only staff-owned profile fields. Before activation,
open `Build and Sign Profile Sync` and replace its secret placeholder with the same intake secret already used
by Workflow 01. The request sends the row's Lead ID, and the backend matches `leads.id` before updating any
profile. A same-phone update stops without writing the Sheet. A changed phone does not alter the database or
cadence; it writes `Phone number changed; needs review` to that exact Sheet row.

The complete variable, API, authentication, failure, and testing contract is in the repository root
`plan.md`.

## Exact Sheet headers

```text
Lead ID | Name | Phone Number | Email | Date Of Birth | Location | Title | Action |
Action Status | Cadence Day | Cadence events | Outcome | Transcript Link | Callback At |
Action Request ID | Action Started At
```

`Action` is a staff command only. `Action Status` is a system result only. `Cadence events` says what ran and
`Outcome` says what happened. Hide and protect the last two recovery columns.

Phone Number identifies a person in the backend, but it is not a safe Sheet row key because the same person
may be entered again. Intake first writes to the exact trigger `row_number`. After that write, intake,
recovery, and AWS status updates target the unique `Action Request ID`. This keeps an older row unchanged when
a later row restarts the same lead.

The intake workflow processes every changed row returned by one Google poll. Its validation and AWS-response
Code nodes loop over `$input.all()`; the signing step reconnects data by `Action Request ID`. Do not change
either Code node to read only `$json` while it is in `Run Once for All Items` mode, because that collapses a
multi-row poll to its first row.

## Commands and date input

Exact Action options:

```text
Start cadence
Restart cadence
Do not contact
```

Enter Date Of Birth as `DD/MM/YYYY`. The intake workflow converts it to ISO `YYYY-MM-DD` before signing and
sending it. A one-word Name is valid. Title and lead_type are aliases for the same value; any
non-empty text is accepted under either name.

## n8n environment

```dotenv
RPT_BACKEND_BASE_URL=https://your-api-domain.example
RPT_N8N_INTAKE_KEY_ID=
RPT_N8N_INTAKE_SECRET=
RPT_N8N_SHEET_KEY_ID=
RPT_N8N_SHEET_WEBHOOK_SECRET=
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
```

The Code nodes use Web Crypto and do not require Node's `crypto` module. If environment access is disabled in
your n8n task runner, replace each `$env` expression inside n8n with its production value before activation;
never commit those secret-bearing exports. The n8n key IDs and secrets must equal their backend counterparts,
but the intake secret and Sheet-webhook secret must be different from each other.

## Endpoints

Intake:

```text
POST {RPT_BACKEND_BASE_URL}/api/v1/integrations/n8n/lead-actions
```

Existing-row profile/phone sync:

```text
POST {RPT_BACKEND_BASE_URL}/api/v1/integrations/n8n/lead-sync
```

Create a separate Google trigger for `Name`, `Phone Number`, `Email`, `Date Of Birth`, `Location`, and `Title`.
Send rows only when Lead ID exists. A same-phone response updates only the database and stops. A changed-phone
response keeps the existing Lead ID and cadence unchanged; update only that triggering row's Action Status to
`Phone number changed; needs review`. Reuse the intake HMAC key and signing logic.

AWS status-update webhook:

```text
POST https://n8n.aibolt.ai/webhook/6c9192c3-ccbf-4530-aa31-88c4ba296d5b
```

Both directions sign `timestamp + "." + exact raw body` with HMAC-SHA256. They use separate secrets.

## Google Update-node fields

- `Write Validation Error`: match `row_number`; write Action Status and Action Started At.
- `Mark Row Processing`: match `row_number`; write Action Status, Action Request ID, and Action Started At.
- `Write Intake Result`: match Action Request ID; write Lead ID, Action Status, and recovery fields.
- `Write Recovery Result`: match Action Request ID; write Lead ID, Action Status, and recovery fields.
- `Update System Columns`: match Action Request ID; write Action Status, Cadence Day, Cadence events, Outcome,
  Transcript Link, Callback At.

After import, reselect the supplied Google OAuth credential in each Google node if n8n displays a warning.
Activate the update-webhook workflow before enabling the backend Sheet worker.

## Action Status lifecycle

```text
Processing: Start cadence
  -> success: Cadence started + Lead ID
  -> temporary failure: Retrying 1/3: Start cadence
  -> temporary failure: Retrying 2/3: Start cadence
  -> third temporary failure: Error: backend unavailable after 3 attempts
  -> permanent 4xx failure: Error: <backend reason>
```

Recovery runs every five minutes and waits at least two minutes after the last attempt. It reuses the same
Action Request ID, so a lost HTTP response cannot create the cadence twice. Existing legacy `Processing:`
rows are also accepted by recovery.

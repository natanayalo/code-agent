# Schedule request and management design

This is the M31-5 design, not a description of shipped dashboard controls. The
dashboard is the first request surface because it already shows authenticated
schedule details and run history. Today, creation and management are available
through the [authenticated API](../README.md#read-only-schedules); the dashboard
view is read-only. Telegram schedule requests remain future work.

## Request to schedule

The first implementation slice will use a structured dashboard form. The
operator supplies a schedule ID and an explicit, allowlisted repository key,
purpose, one or more weekdays, local time and IANA timezone. An optional branch
may be supplied. The supported purposes are `dependency_review`,
`health_summary`, and `evidence_collection`; the last also requires the
`operational_incidents` source and a lookback of 1–168 hours. The repository
selects the execution workspace and does not filter operational incidents.

The form maps those values to version 1 of the existing `ScheduleSpec`: weekdays
are distinct, time is `HH:MM`, and timezone is an IANA zone. The ID is 1–200
ASCII letters, digits, hyphens, underscores or periods. The server remains the
authority for validation and repository allowlisting. No timezone, repository,
purpose, or approval to activate is inferred from browser settings or prose.

Before sending a request, show the complete specification and schedule ID for
review, including the local time and named timezone, branch (or its absence),
and evidence configuration where applicable. Explain that creation is paused
and that activation is a separate action. Only an explicit create action sends
the existing authenticated `PUT /schedules/{id}`. Show the returned state and
specification: an identical retry can return an existing schedule that is
already active. A new schedule is paused; creation never implicitly resumes or
triggers it. Temporal continues to interpret the calendar in the named timezone.

The form retains entered values and identifies invalid fields when validation
fails, including an unallowlisted repository. A conflicting ID must be shown
as a conflict, with the operator able to choose a different ID; do not overwrite
the existing schedule. If Temporal is unavailable or the response is uncertain,
show that the outcome is unknown, preserve the request, and let the operator
refresh or retry the same ID and specification. Do not report success or silently
use another ID. Identical requests are idempotent; a changed specification
requires a new ID.

## Separate implementation slices

1. **Structured creation:** implement the form, review step, and paused creation
   using the existing API. Keep management controls out of this slice.
2. **Management controls:** add distinct pause, resume, and trigger actions to
   the schedule view using the existing authenticated endpoints. Show the
   returned state and refresh schedule and run data. Explain that a manual
   trigger can run while paused; require an explicit action for each operation.
   On errors or uncertain responses, retain the visible context, report the
   outcome as unknown where appropriate, and refresh before another action.
3. **Natural-language requests:** allow a dashboard request to propose only the
   existing structured fields and purposes. Ask for missing or ambiguous values,
   preserve the operator's explicit timezone and intent, and route the proposal
   through the same editable review and explicit create action. Interpretation
   alone must never create, resume, or trigger a schedule.

All slices retain the existing read-only scheduled purposes and Temporal
lifecycle. They introduce neither a second scheduler nor unattended mutable
work. Each slice updates operator documentation when shipped; the parent
[M31-5 issue](https://github.com/natanayalo/code-agent/issues/454) remains open
until its implementation acceptance criteria are met.

## Acceptance and verification for implementation

- Structured creation needs component coverage for required fields, review,
  explicit submission, and retained input after errors; API/service contract
  coverage for the exact `ScheduleSpec` and create response; and an interaction
  smoke covering invalid input, conflict, Temporal unavailability, retry, and
  an existing active schedule returned by an identical request.
- Management needs component coverage for each distinct action and displayed
  state; API/service contract coverage for pause, resume, and trigger; and an
  interaction smoke covering success, failure or uncertain outcome, refresh,
  recovery, and triggering a paused schedule.
- Natural-language input needs component coverage for incomplete and ambiguous
  proposals and explicit review; API/service contract coverage for its eventual
  interpretation boundary and unchanged schedule submission; and an interaction
  smoke proving that interpretation alone causes no schedule operation and that
  error recovery preserves operator intent.
- Each dashboard-control slice also runs the repository's dashboard coverage
  gate and the applicable critical-path checks in `AGENTS.md`. The design does
  not change runtime behavior or public interfaces.

import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../services/api';
import { ScheduleSpec } from '../types/metrics';

const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

export function ScheduleCreateForm() {
  const client = useQueryClient();
  const [id, setId] = React.useState('');
  const [repo, setRepo] = React.useState('');
  const [branch, setBranch] = React.useState('');
  const [purpose, setPurpose] = React.useState<ScheduleSpec['purpose'] | ''>('');
  const [days, setDays] = React.useState<string[]>([]);
  const [time, setTime] = React.useState('');
  const [timezone, setTimezone] = React.useState('');
  const [lookback, setLookback] = React.useState('24');
  const [review, setReview] = React.useState(false);
  const [dayError, setDayError] = React.useState(false);
  const mutation = useMutation({
    mutationFn: (spec: ScheduleSpec) => api.createSchedule(id, spec),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['dependency-review-schedules'] }); },
  });
  const spec: ScheduleSpec = {
    version: 1, purpose: purpose || 'dependency_review', repo_key: repo, branch: branch || null,
    weekdays: days, time, timezone,
    ...(purpose === 'evidence_collection' ? { evidence: { source: 'operational_incidents' as const, lookback_hours: Number(lookback) } } : {}),
  };
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!days.length) { setDayError(true); return; }
    setDayError(false);
    setReview(true);
    mutation.reset();
  }
  return <div className="metric-detail-card card schedule-create">
    <h3>Create a schedule</h3>
    {!review ? <form onSubmit={submit}>
      <label>Schedule ID <input required maxLength={200} pattern="[A-Za-z0-9_.-]+" value={id} onChange={(e) => setId(e.target.value)} /></label>
      <label>Repository key <input required maxLength={255} pattern="\S+" value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
      <label>Branch (optional) <input maxLength={255} pattern="\S+" value={branch} onChange={(e) => setBranch(e.target.value)} /></label>
      <label>Purpose <select required value={purpose} onChange={(e) => setPurpose(e.target.value as ScheduleSpec['purpose'] | '')}>
        <option value="">Choose a purpose</option><option value="dependency_review">Dependency review</option><option value="health_summary">Health summary</option><option value="evidence_collection">Evidence collection</option>
      </select></label>
      {purpose === 'evidence_collection' ? <label>Incident lookback (hours) <input type="number" required min="1" max="168" step="1" value={lookback} onChange={(e) => setLookback(e.target.value)} /></label> : null}
      <fieldset><legend>Weekdays</legend>{DAYS.map((day) => <label key={day}><input type="checkbox" checked={days.includes(day)} onChange={(e) => setDays(e.target.checked ? [...days, day] : days.filter((item) => item !== day))} /> {day}</label>)}</fieldset>
      {dayError ? <p role="alert">Select at least one weekday.</p> : null}
      <label>Local time <input type="time" required value={time} onChange={(e) => setTime(e.target.value)} /></label>
      <label>IANA timezone <input required maxLength={255} value={timezone} onChange={(e) => setTimezone(e.target.value)} placeholder="Asia/Jerusalem" /></label>
      <button className="btn-secondary" type="submit">Review schedule</button>
    </form> : <div>
      <h4>Review schedule</h4>
      <dl className="schedule-spec">
        <div><dt>Specification version</dt><dd>1</dd></div>
        <div><dt>Schedule ID</dt><dd>{id}</dd></div><div><dt>Repository key</dt><dd>{spec.repo_key}</dd></div>
        <div><dt>Branch</dt><dd>{spec.branch || 'Default branch'}</dd></div><div><dt>Purpose</dt><dd>{spec.purpose.replaceAll('_', ' ')}</dd></div>
        <div><dt>Schedule</dt><dd>{days.join(', ')} at {time} ({timezone})</dd></div>
        {spec.evidence ? <div><dt>Evidence</dt><dd>Operational incidents · {lookback} hours</dd></div> : null}
      </dl>
      <p>New schedules are created paused. Activation is a separate action.</p>
      {mutation.error ? <p role="alert">{mutation.error instanceof ApiError && mutation.error.status === 409 ? 'This schedule ID conflicts with an existing specification. Choose a different ID.' : mutation.error instanceof ApiError && [400, 422].includes(mutation.error.status) ? `Invalid request: ${mutation.error.message}` : 'The outcome is unknown. Refresh the schedule list or retry this same request.'}</p> : null}
      {mutation.data ? <p role="status">Schedule {mutation.data.id} returned as {mutation.data.paused ? 'paused' : 'active'}. Check its details below.</p> : null}
      <button className="btn-secondary" type="button" disabled={mutation.isPending} onClick={() => { setReview(false); mutation.reset(); }}>Edit</button>{' '}
      <button className="btn-primary" type="button" disabled={mutation.isPending || Boolean(mutation.data)} onClick={() => mutation.mutate(spec)}>{mutation.isPending ? 'Creating…' : 'Create schedule'}</button>
    </div>}
  </div>;
}

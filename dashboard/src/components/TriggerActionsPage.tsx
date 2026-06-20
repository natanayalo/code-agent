import React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  Loader2,
  Radar,
  Send,
} from "lucide-react";
import { api } from "../services/api";
import {
  ScoutTriggerRequest,
  TaskSnapshot,
  TaskSpecType,
  TaskSubmissionRequest,
  WorkerType,
  WORKER_OPTIONS,
} from "../types/task";
import { DashboardLayout } from "./layout/DashboardLayout";
import { formatLabel } from "../utils/formatters";

type TriggerTab = "task" | "scout";
type DashboardTaskType = Exclude<TaskSpecType, "scout">;
type WorkerSelection = WorkerType | "";

const DASHBOARD_SESSION = {
  channel: "dashboard",
  external_user_id: "dashboard:operator",
  external_thread_id: "dashboard-triggers",
  display_name: "Dashboard Operator",
};

const MAX_TASK_PRIORITY = 2_147_483_647;

const TASK_TYPE_OPTIONS: Array<{ label: string; value: DashboardTaskType }> = [
  { label: "Feature", value: "feature" },
  { label: "Bugfix", value: "bugfix" },
  { label: "Investigation", value: "investigation" },
  { label: "Maintenance", value: "maintenance" },
  { label: "Docs", value: "docs" },
  { label: "Refactor", value: "refactor" },
  { label: "Review Fix", value: "review_fix" },
];

function normalizeOptional(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function getErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function TriggerResult({
  task,
  label,
}: {
  task: TaskSnapshot | null;
  label: string;
}) {
  return (
    <div className={task ? "trigger-result" : undefined} role="status">
      {task ? (
        <>
          <CheckCircle2 size={18} />
          <span>{label}</span>
          <code>{task.task_id}</code>
        </>
      ) : null}
    </div>
  );
}

export function TriggerActionsPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = React.useState<TriggerTab>("task");
  const [taskText, setTaskText] = React.useState("");
  const [repoUrl, setRepoUrl] = React.useState("");
  const [branch, setBranch] = React.useState("");
  const [priority, setPriority] = React.useState("");
  const [taskType, setTaskType] = React.useState<DashboardTaskType>("feature");
  const [workerOverride, setWorkerOverride] =
    React.useState<WorkerSelection>("");
  const [taskError, setTaskError] = React.useState<string | null>(null);
  const [scoutError, setScoutError] = React.useState<string | null>(null);
  const [lastSubmittedTask, setLastSubmittedTask] =
    React.useState<TaskSnapshot | null>(null);
  const [lastScoutTask, setLastScoutTask] = React.useState<TaskSnapshot | null>(
    null,
  );

  const [scoutMode, setScoutMode] = React.useState<
    "repo" | "research" | "deep"
  >("repo");
  const [scoutRepoKey, setScoutRepoKey] = React.useState("");
  const [scoutBranch, setScoutBranch] = React.useState("");
  const [scoutFocus, setScoutFocus] = React.useState("");
  const [scoutDepth, setScoutDepth] = React.useState<
    "shallow" | "standard" | "deep"
  >("standard");
  const [scoutMaxProposals, setScoutMaxProposals] = React.useState("5");

  const submitTaskMutation = useMutation({
    mutationFn: (payload: TaskSubmissionRequest) => api.submitTask(payload),
    onSuccess: (task) => {
      setLastSubmittedTask(task);
      setTaskError(null);
      setTaskText("");
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (error) => {
      setTaskError(getErrorMessage(error, "Failed to submit task."));
    },
  });

  const triggerScoutMutation = useMutation({
    mutationFn: (payload?: ScoutTriggerRequest) =>
      api.triggerScoutTask(payload),
    onSuccess: (task) => {
      setLastScoutTask(task);
      setScoutError(null);
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (error) => {
      setScoutError(getErrorMessage(error, "Failed to trigger scout task."));
    },
  });

  const handleTaskSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitTaskMutation.isPending) {
      return;
    }
    setTaskError(null);
    setLastSubmittedTask(null);

    const normalizedTaskText = taskText.trim();
    if (!normalizedTaskText) {
      setTaskError("Task text is required.");
      return;
    }

    const payload: TaskSubmissionRequest = {
      task_text: normalizedTaskText,
      constraints: {
        task_type: taskType,
        trigger_source: "dashboard",
      },
      session: DASHBOARD_SESSION,
    };
    const normalizedPriority = priority.trim();
    if (normalizedPriority) {
      const parsedPriority = Number(normalizedPriority);
      if (
        !Number.isInteger(parsedPriority) ||
        parsedPriority < 0 ||
        parsedPriority > MAX_TASK_PRIORITY
      ) {
        setTaskError(
          `Priority must be a whole number between 0 and ${MAX_TASK_PRIORITY}.`,
        );
        return;
      }
      payload.priority = parsedPriority;
    }
    const normalizedRepoUrl = normalizeOptional(repoUrl);
    const normalizedBranch = normalizeOptional(branch);
    if (normalizedRepoUrl) {
      payload.repo_url = normalizedRepoUrl;
    }
    if (normalizedBranch) {
      payload.branch = normalizedBranch;
    }
    if (workerOverride) {
      payload.worker_override = workerOverride;
    }

    submitTaskMutation.mutate(payload);
  };

  const handleScoutSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (triggerScoutMutation.isPending) {
      return;
    }
    setScoutError(null);
    setLastScoutTask(null);

    const parsedMaxProposals = Number(scoutMaxProposals);
    if (
      !Number.isInteger(parsedMaxProposals) ||
      parsedMaxProposals < 1 ||
      parsedMaxProposals > 20
    ) {
      setScoutError("Max Proposals must be a whole number between 1 and 20.");
      return;
    }

    const payload: ScoutTriggerRequest = {
      mode: scoutMode,
      depth: scoutDepth,
      max_proposals: parsedMaxProposals,
    };

    const normalizedRepoKey = normalizeOptional(scoutRepoKey);
    if (normalizedRepoKey) payload.repo_key = normalizedRepoKey;

    const normalizedBranch = normalizeOptional(scoutBranch);
    if (normalizedBranch) payload.branch = normalizedBranch;

    const normalizedFocus = normalizeOptional(scoutFocus);
    if (normalizedFocus) payload.focus = normalizedFocus;

    if (scoutMode === "research" && !normalizedFocus) {
      setScoutError("Focus is required for research mode.");
      return;
    }

    triggerScoutMutation.mutate(payload);
  };

  return (
    <DashboardLayout>
      <div className="trigger-actions-page">
        <header className="metrics-header trigger-actions-header">
          <div className="header-title">
            <Send className="header-icon" size={24} />
            <h2>Triggers</h2>
          </div>
        </header>

        <div
          className="trigger-tab-list"
          role="tablist"
          aria-label="Trigger actions"
        >
          <button
            type="button"
            id="trigger-tab-task"
            role="tab"
            aria-selected={activeTab === "task"}
            aria-controls="trigger-panel-task"
            className={`trigger-tab-button ${activeTab === "task" ? "active" : ""}`}
            onClick={() => setActiveTab("task")}
          >
            <ClipboardList size={16} />
            <span>Task</span>
          </button>
          <button
            type="button"
            id="trigger-tab-scout"
            role="tab"
            aria-selected={activeTab === "scout"}
            aria-controls="trigger-panel-scout"
            className={`trigger-tab-button ${activeTab === "scout" ? "active" : ""}`}
            onClick={() => setActiveTab("scout")}
          >
            <Radar size={16} />
            <span>Scout</span>
          </button>
        </div>

        {activeTab === "task" ? (
          <section
            id="trigger-panel-task"
            role="tabpanel"
            aria-labelledby="trigger-tab-task"
            className="trigger-panel"
          >
            <form className="trigger-form" onSubmit={handleTaskSubmit}>
              <label htmlFor="trigger-task-text">Task text</label>
              <textarea
                id="trigger-task-text"
                value={taskText}
                onChange={(event) => setTaskText(event.target.value)}
                rows={5}
                required
              />

              <div className="trigger-form-row">
                <div className="trigger-form-field">
                  <label htmlFor="trigger-repo-url">Repository URL</label>
                  <input
                    id="trigger-repo-url"
                    value={repoUrl}
                    onChange={(event) => setRepoUrl(event.target.value)}
                  />
                </div>
                <div className="trigger-form-field">
                  <label htmlFor="trigger-branch">Branch</label>
                  <input
                    id="trigger-branch"
                    value={branch}
                    onChange={(event) => setBranch(event.target.value)}
                  />
                </div>
              </div>

              <div className="trigger-form-row trigger-form-row-three">
                <div className="trigger-form-field">
                  <label htmlFor="trigger-task-type">Task type</label>
                  <select
                    id="trigger-task-type"
                    value={taskType}
                    onChange={(event) =>
                      setTaskType(event.target.value as DashboardTaskType)
                    }
                  >
                    {TASK_TYPE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="trigger-form-field">
                  <label htmlFor="trigger-worker">Worker</label>
                  <select
                    id="trigger-worker"
                    value={workerOverride}
                    onChange={(event) =>
                      setWorkerOverride(event.target.value as WorkerSelection)
                    }
                  >
                    <option value="">Auto</option>
                    {WORKER_OPTIONS.map((worker) => (
                      <option key={worker} value={worker}>
                        {formatLabel(worker)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="trigger-form-field trigger-priority-field">
                  <label htmlFor="trigger-priority">Priority</label>
                  <input
                    id="trigger-priority"
                    type="number"
                    min="0"
                    max={MAX_TASK_PRIORITY}
                    step="1"
                    value={priority}
                    onChange={(event) => setPriority(event.target.value)}
                  />
                </div>
              </div>

              {taskError ? (
                <div className="error-banner trigger-error-banner" role="alert">
                  <AlertTriangle size={16} />
                  <span>{taskError}</span>
                </div>
              ) : null}

              <div className="trigger-actions-footer">
                <button
                  type="submit"
                  className="button button-success trigger-primary-button"
                  disabled={submitTaskMutation.isPending}
                >
                  {submitTaskMutation.isPending ? (
                    <Loader2 className="spin" size={16} />
                  ) : (
                    <Send size={16} />
                  )}
                  <span>
                    {submitTaskMutation.isPending
                      ? "Queueing..."
                      : "Queue Task"}
                  </span>
                </button>
                <TriggerResult task={lastSubmittedTask} label="Task queued" />
              </div>
            </form>
          </section>
        ) : (
          <section
            id="trigger-panel-scout"
            role="tabpanel"
            aria-labelledby="trigger-tab-scout"
            className="trigger-panel"
          >
            <form className="trigger-form" onSubmit={handleScoutSubmit}>
              <div className="trigger-form-row">
                <div className="trigger-form-field">
                  <label htmlFor="scout-mode">Mode</label>
                  <select
                    id="scout-mode"
                    value={scoutMode}
                    onChange={(e) =>
                      setScoutMode(
                        e.target.value as "repo" | "research" | "deep",
                      )
                    }
                  >
                    <option value="repo">Repo</option>
                    <option value="research">Research</option>
                    <option value="deep">Deep</option>
                  </select>
                </div>
                <div className="trigger-form-field">
                  <label htmlFor="scout-depth">Depth</label>
                  <select
                    id="scout-depth"
                    value={scoutDepth}
                    onChange={(e) =>
                      setScoutDepth(
                        e.target.value as "shallow" | "standard" | "deep",
                      )
                    }
                  >
                    <option value="shallow">Shallow</option>
                    <option value="standard">Standard</option>
                    <option value="deep">Deep</option>
                  </select>
                </div>
              </div>

              <div className="trigger-form-row">
                <div className="trigger-form-field">
                  <label htmlFor="scout-repo-key">Repo Key (Optional)</label>
                  <input
                    id="scout-repo-key"
                    value={scoutRepoKey}
                    onChange={(e) => setScoutRepoKey(e.target.value)}
                  />
                </div>
                <div className="trigger-form-field">
                  <label htmlFor="scout-branch">Branch (Optional)</label>
                  <input
                    id="scout-branch"
                    value={scoutBranch}
                    onChange={(e) => setScoutBranch(e.target.value)}
                  />
                </div>
              </div>

              <div className="trigger-form-field">
                <label htmlFor="scout-focus">
                  Focus{" "}
                  {scoutMode === "research"
                    ? "(Required for research mode)"
                    : "(Optional)"}
                </label>
                <input
                  id="scout-focus"
                  value={scoutFocus}
                  onChange={(e) => setScoutFocus(e.target.value)}
                  required={scoutMode === "research"}
                />
              </div>

              <div className="trigger-form-field trigger-priority-field">
                <label htmlFor="scout-max-proposals">
                  Max Proposals (1-20)
                </label>
                <input
                  id="scout-max-proposals"
                  type="number"
                  min="1"
                  max="20"
                  step="1"
                  value={scoutMaxProposals}
                  onChange={(e) => setScoutMaxProposals(e.target.value)}
                  required
                />
              </div>

              {scoutError ? (
                <div className="error-banner trigger-error-banner" role="alert">
                  <AlertTriangle size={16} />
                  <span>{scoutError}</span>
                </div>
              ) : null}

              <div className="trigger-actions-footer">
                <button
                  type="submit"
                  className="button button-success trigger-primary-button"
                  disabled={triggerScoutMutation.isPending}
                >
                  {triggerScoutMutation.isPending ? (
                    <Loader2 className="spin" size={16} />
                  ) : (
                    <Radar size={16} />
                  )}
                  <span>
                    {triggerScoutMutation.isPending
                      ? "Triggering..."
                      : "Trigger Scout"}
                  </span>
                </button>
                <TriggerResult task={lastScoutTask} label="Scout queued" />
              </div>
            </form>
          </section>
        )}
      </div>
    </DashboardLayout>
  );
}

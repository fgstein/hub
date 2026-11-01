/** An external run: an MLflow run TrainHub did not launch, found in the project's experiments. */
import { Show } from "solid-js"
import { elapsedSeconds, formatDateTime, formatDuration } from "~/lib/format"
import { externalTraining } from "~/lib/mlflow"
import { markSeen, project, removeExternal, state } from "~/stores/app"
import { now } from "~/stores/clock"
import { IconTrash } from "./Icons"
import { LookinLinks } from "./LookinLinks"
import { MlflowProgress, SeenButton, Tip, TRAINING_COLOR } from "./RunRow"
import { trainingLabel } from "./StatusDot"

export function ExternalRunRow(props: { id: string }) {
  const run = () => state.external[props.id]
  return (
    <Show when={run()}>
      <ExternalRunLine id={props.id} />
    </Show>
  )
}

function ExternalRunLine(props: { id: string }) {
  const run = () => state.external[props.id]!
  const ml = () => run().mlflow
  const training = () => externalTraining(ml().status, ml().stale ?? false)
  const elapsed = () =>
    ml().started_at
      ? formatDuration(elapsedSeconds({ started_at: ml().started_at, finished_at: ml().finished_at }, now()))
      : "—"
  return (
    <div
      class="anim-in flex h-12 items-center gap-3 border-b border-line-weaker px-4"
      data-testid="external-run-row"
      data-run-id={props.id}
      data-training={training()}
    >
      <span class="relative flex size-4 shrink-0 items-center justify-center">
        <span
          class="dot"
          style={{ "--dot-color": TRAINING_COLOR[training()] }}
          data-pulse={run().active && training() === "healthy" ? "" : undefined}
          role="img"
          aria-label={trainingLabel(training())}
        />
      </span>
      <Tip
        class="w-[68px] shrink-0 font-mono text-sm tabular text-fg-strong"
        content={
          <div class="space-y-0.5 font-mono">
            <div>{trainingLabel(training())}</div>
            <div>MLflow run {run().id}</div>
            <div>started: {formatDateTime(ml().started_at)}</div>
            <Show when={ml().finished_at}>
              <div>finished: {formatDateTime(ml().finished_at)}</div>
            </Show>
            <div>found by TrainHub: {formatDateTime(run().discovered_at)}</div>
          </div>
        }
      >
        <span data-testid="run-elapsed">{elapsed()}</span>
      </Tip>
      <div class="flex min-w-0 flex-1 items-center gap-1.5 whitespace-nowrap font-mono text-sm text-fg">
        <Tip
          class="shrink-0 rounded bg-surface px-1.5 font-sans text-xs text-fg-weak"
          content="MLflow run not launched by TrainHub (found in an experiment of this project): no machine, container nor logs known, only what MLflow logged"
        >
          <span data-testid="external-badge">external</span>
        </Tip>
        <Tip class="min-w-0 truncate text-fg-strong" content={run().name ?? run().id}>
          {run().name ?? run().id.slice(0, 8)}
        </Tip>
        <Show when={run().user}>
          <span class="text-fg-weaker">·</span>
          <Tip class="min-w-0 truncate" content="MLflow user (mlflow.user)">
            {run().user}
          </Tip>
        </Show>
        <Show when={run().experiment}>
          <span class="text-fg-weaker">·</span>
          <Tip class="min-w-0 truncate text-fg-weak" content="MLflow experiment">
            {run().experiment}
          </Tip>
        </Show>
      </div>
      <MlflowProgress run={run()} color={TRAINING_COLOR[training()]} />
      <LookinLinks
        mlflowRunId={run().id}
        button={!!project(run().project_id)?.accepts_checkpoint && !run().active}
      />
      <Show when={run().stale_seen_at && !run().unseen_failure}>
        <Tip
          class="shrink-0 text-xs text-fg-weak"
          content={`Stalled, marked as seen ${formatDateTime(run().stale_seen_at)}: back among the active runs if it logs again`}
        >
          <span data-testid="external-stale-seen">stalled, seen</span>
        </Tip>
      </Show>
      <Show when={run().unseen_failure}>
        <SeenButton onClick={() => markSeen(props.id, true)} />
      </Show>
      <Show when={!run().unseen_failure && run().active && ml().stale && !run().stale_seen_at}>
        <SeenButton
          onClick={() => markSeen(props.id, true)}
          title="Stalled: still running in MLflow but no new metric for a while (often a killed process MLflow was never told about). Seen moves it to the finished runs; it comes back if it logs again."
        />
      </Show>
      <button
        type="button"
        class="flex size-7 shrink-0 items-center justify-center rounded-md text-icon hover:bg-surface-hover hover:text-icon-hover"
        title="Remove from TrainHub: it does not come back (nothing changes in MLflow)"
        aria-label="Remove from TrainHub"
        onClick={() => removeExternal(props.id)}
        data-testid="external-remove"
      >
        <IconTrash size={13} />
      </button>
    </div>
  )
}

import type { Training } from "~/lib/mlflow"
import type { RunStatus } from "~/lib/types"

const STYLE: Record<RunStatus, { color: string; pulse?: boolean; blink?: boolean; label: string }> = {
  launching: { color: "var(--status-orange)", pulse: true, label: "launching" },
  running: { color: "var(--status-green)", pulse: true, label: "running" },
  paused: { color: "var(--status-orange)", label: "paused" },
  stopping: { color: "var(--status-orange)", pulse: true, label: "stopping" },
  succeeded: { color: "var(--status-green)", label: "succeeded" },
  failed: { color: "var(--status-red)", label: "failed" },
  // Open point of the spec settled: grey for a deliberate stop.
  stopped: { color: "var(--status-grey)", label: "stopped" },
  unreachable: { color: "var(--status-red)", blink: true, label: "machine unreachable" },
}

/** Color given by the MLflow training state (overrides the docker status color). */
const TRAINING: Record<Training, { color: string; label: string }> = {
  healthy: { color: "var(--status-green)", label: "training OK (MLflow)" },
  failed: { color: "var(--status-red)", label: "training failed (MLflow)" },
  finished: { color: "var(--status-blue)", label: "training finished (MLflow)" },
}

export const statusLabel = (s: RunStatus) => STYLE[s].label
export const trainingLabel = (t: Training) => TRAINING[t].label

export function StatusDot(props: { status: RunStatus | "draft"; training?: Training | null }) {
  const st = () => {
    if (props.status === "draft") return undefined
    const base = STYLE[props.status]
    const t = props.training ? TRAINING[props.training] : undefined
    return t ? { ...base, color: t.color, label: `${base.label} · ${t.label}` } : base
  }
  return (
    <span
      class="relative flex size-4 shrink-0 items-center justify-center"
      data-status={props.status}
      data-training={props.training ?? undefined}
    >
      <span
        class="dot"
        style={{ "--dot-color": st()?.color }}
        data-pulse={st()?.pulse ? "" : undefined}
        data-blink={st()?.blink ? "" : undefined}
        data-hollow={props.status === "draft" ? "" : undefined}
        role="img"
        aria-label={st()?.label ?? "draft"}
      />
      {props.status === "succeeded" && props.training !== "failed" && (
        <span
          class="absolute -right-1.5 -top-1 text-[9px] leading-none"
          classList={{
            "text-st-green": !props.training,
            "text-st-blue": props.training === "finished",
          }}
        >
          ✓
        </span>
      )}
      {props.status === "stopped" && (
        <span class="absolute -right-1.5 -top-1 text-[7px] leading-none text-st-grey">■</span>
      )}
    </span>
  )
}

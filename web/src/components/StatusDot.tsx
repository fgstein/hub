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

export const statusLabel = (s: RunStatus) => STYLE[s].label

export function StatusDot(props: { status: RunStatus | "draft" }) {
  const st = () => (props.status === "draft" ? undefined : STYLE[props.status])
  return (
    <span class="relative flex size-4 shrink-0 items-center justify-center" data-status={props.status}>
      <span
        class="dot"
        style={{ "--dot-color": st()?.color }}
        data-pulse={st()?.pulse ? "" : undefined}
        data-blink={st()?.blink ? "" : undefined}
        data-hollow={props.status === "draft" ? "" : undefined}
        role="img"
        aria-label={st()?.label ?? "draft"}
      />
      {props.status === "succeeded" && (
        <span class="absolute -right-1.5 -top-1 text-[9px] leading-none text-st-green">✓</span>
      )}
      {props.status === "stopped" && (
        <span class="absolute -right-1.5 -top-1 text-[7px] leading-none text-st-grey">■</span>
      )}
    </span>
  )
}

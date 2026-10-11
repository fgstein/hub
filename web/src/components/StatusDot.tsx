import type { RunStatus } from "~/lib/types"

const STYLE: Record<RunStatus, { color: string; pulse?: boolean; blink?: boolean; label: string }> = {
  launching: { color: "var(--status-orange)", pulse: true, label: "lancement" },
  running: { color: "var(--status-green)", pulse: true, label: "en cours" },
  paused: { color: "var(--status-orange)", label: "en pause" },
  stopping: { color: "var(--status-orange)", pulse: true, label: "arrêt en cours" },
  succeeded: { color: "var(--status-green)", label: "terminé avec succès" },
  failed: { color: "var(--status-red)", label: "échec" },
  // Point ouvert de la spec tranché : gris pour un arrêt volontaire.
  stopped: { color: "var(--status-grey)", label: "arrêté" },
  unreachable: { color: "var(--status-red)", blink: true, label: "machine injoignable" },
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
        aria-label={st()?.label ?? "brouillon"}
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

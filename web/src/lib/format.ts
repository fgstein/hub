import type { RunView } from "./types"

/** Format a duration: 12s, 4m 05s, 3h 12m, 2d 04h (same as the Python core). */
export function formatDuration(seconds: number): string {
  let s = Math.floor(Math.max(0, seconds))
  if (s < 60) return `${s}s`
  let m = Math.floor(s / 60)
  s %= 60
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`
  let h = Math.floor(m / 60)
  m %= 60
  if (h < 24) return `${h}h ${String(m).padStart(2, "0")}m`
  const d = Math.floor(h / 24)
  h %= 24
  return `${d}d ${String(h).padStart(2, "0")}h`
}

const ts = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN)

/** Elapsed time: now - started_at while active, else finished_at - started_at. */
export function elapsedSeconds(run: Pick<RunView, "started_at" | "finished_at">, nowMs: number): number {
  const start = ts(run.started_at)
  if (Number.isNaN(start)) return 0
  const end = run.finished_at ? ts(run.finished_at) : nowMs
  return Math.max(0, (end - start) / 1000)
}

/** Cumulative paused duration, including the current pause. */
export function pausedSeconds(run: Pick<RunView, "paused_total_s" | "paused_at">, nowMs: number): number {
  let total = run.paused_total_s ?? 0
  const p = ts(run.paused_at)
  if (!Number.isNaN(p)) total += Math.max(0, (nowMs - p) / 1000)
  return total
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}

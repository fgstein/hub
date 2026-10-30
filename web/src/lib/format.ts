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

/** Size in bytes, decimal units like a file manager: 412 MB, 3.7 GB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—"
  const units = ["B", "kB", "MB", "GB", "TB", "PB"]
  let v = Math.max(0, bytes)
  let i = 0
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000
    i++
  }
  const digits = i === 0 || v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${v.toFixed(digits)} ${units[i]}`
}

/** Age of a date: just now, 5 min ago, 3 h ago, 4 d ago, then the date. */
export function formatAgo(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return "—"
  const s = Math.max(0, (nowMs - Date.parse(iso)) / 1000)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  if (s < 30 * 86400) return `${Math.floor(s / 86400)} d ago`
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })
}

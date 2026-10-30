import type { MachineLoad } from "./types"

/** Usage → color of a load cell: one hue, from --load-empty (0 %) to --load-full (100 %). */
export function loadColor(pct: number | null | undefined): string {
  if (pct == null) return "var(--load-empty)"
  const p = Math.round(Math.max(0, Math.min(100, pct)))
  return `color-mix(in oklab, var(--load-full) ${p}%, var(--load-empty))`
}

export const percent = (used: number | null | undefined, total: number | null | undefined) =>
  used != null && total ? Math.min(100, (100 * used) / total) : null

export interface LoadSummary {
  machines: number
  reachable: number
  gpus: number
  /** GPUs with at least one compute process (any user). */
  busyGpus: number
  cores: number
  /** Average CPU usage of the reachable machines, weighted by their number of cores. */
  cpuPct: number | null
}

export function summarize(loads: (MachineLoad | undefined)[]): LoadSummary {
  const s: LoadSummary = {
    machines: loads.length,
    reachable: 0,
    gpus: 0,
    busyGpus: 0,
    cores: 0,
    cpuPct: null,
  }
  let busyCores = 0
  for (const l of loads) {
    if (!l?.reachable) continue
    s.reachable++
    s.gpus += l.gpus?.length ?? 0
    s.busyGpus += (l.gpus ?? []).filter((g) => g.processes > 0).length
    if (l.cpu) {
      s.cores += l.cpu.cores
      busyCores += (l.cpu.usage_pct * l.cpu.cores) / 100
    }
  }
  if (s.cores) s.cpuPct = (100 * busyCores) / s.cores
  return s
}

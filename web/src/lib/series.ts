/** Series: the same run repeated several times in a row, in one container. */
import type { Series, SeriesRequest } from "./types"

export const MAX_SERIES = 100
/** Default range of a random parameter (non-negative 32-bit integer, a usual seed range). */
export const DEFAULT_MIN = 0
export const DEFAULT_MAX = 2 ** 31 - 1

/** Range of a random parameter as typed in the draft (empty: default bound). */
export interface RangeDraft {
  min: string
  max: string
}

const INT_RE = /^-?\d+$/

function bound(text: string, fallback: number): number | null {
  const t = text.trim()
  if (!t) return fallback
  return INT_RE.test(t) ? Number(t) : null
}

/** Number of repetitions typed in the draft (empty: 1), or null if invalid. */
export function parseCount(text: string): number | null {
  const t = text.trim()
  if (!t) return 1
  if (!/^\d+$/.test(t)) return null
  const n = Number(t)
  return n >= 1 && n <= MAX_SERIES ? n : null
}

/**
 * Series request of a draft; ``request`` is null for a single run (one repetition, nothing
 * random). ``errors`` is keyed by field: ``count`` or a parameter name.
 */
export function seriesRequest(
  count: string,
  random: Record<string, RangeDraft | undefined>,
  continueOnFailure: boolean,
  params: readonly string[],
): { request: SeriesRequest | null; errors: Record<string, string> } {
  const errors: Record<string, string> = {}
  const n = parseCount(count)
  if (n == null) errors.count = `number of runs: integer from 1 to ${MAX_SERIES}`
  const ranges: SeriesRequest["random"] = {}
  for (const name of params) {
    const r = random[name]
    if (!r) continue
    const min = bound(r.min, DEFAULT_MIN)
    const max = bound(r.max, DEFAULT_MAX)
    if (min == null || max == null) errors[name] = "integer bounds expected"
    else if (min > max) errors[name] = `empty range: ${min} > ${max}`
    else ranges[name] = { min, max }
  }
  if (Object.keys(errors).length || n == null) return { request: null, errors }
  if (n === 1 && !Object.keys(ranges).length) return { request: null, errors }
  return {
    request: { count: n, random: ranges, continue_on_failure: continueOnFailure },
    errors,
  }
}

/** Draft fields of a run's series (“Relaunch…”): same count and ranges, new draws. */
export function draftSeries(series: Series | null | undefined): {
  count: string
  random: Record<string, RangeDraft>
  continueOnFailure: boolean
} {
  if (!series) return { count: "", random: {}, continueOnFailure: false }
  const random: Record<string, RangeDraft> = {}
  for (const [name, [min, max]] of Object.entries(series.random ?? {})) {
    random[name] = {
      min: min === DEFAULT_MIN ? "" : String(min),
      max: max === DEFAULT_MAX ? "" : String(max),
    }
  }
  return {
    count: String(series.count),
    random,
    continueOnFailure: series.continue_on_failure ?? false,
  }
}

export type RepetitionState = "done" | "failed" | "running" | "pending" | "skipped"

/** State of each repetition, for a run that is ``active`` or not. */
export function repetitions(series: Series, active: boolean): RepetitionState[] {
  const codes = series.exit_codes ?? []
  return Array.from({ length: series.count }, (_, i) => {
    const code = codes[i]
    if (code != null) return code === 0 ? "done" : "failed"
    if (i + 1 === series.current && active) return "running"
    return active ? "pending" : "skipped"
  })
}

/** ``seed=4821 lr=0.1`` */
export function describeValues(values: Record<string, string> | undefined): string {
  return Object.entries(values ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join(" ")
}

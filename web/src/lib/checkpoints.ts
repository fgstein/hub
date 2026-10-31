/** Checkpoints tab: filtering, sorting and selection over a source listing (pure functions). */
import type {
  CheckpointFile,
  CheckpointGroup,
  CheckpointInference,
  CheckpointListing,
  CheckpointRun,
  MlflowRunRef,
} from "./types"

export type SortKey = "date" | "name" | "size"

export interface ViewOptions {
  query: string
  sort: SortKey
  hideEmpty: boolean
}

/** A run as displayed: its groups and “other” files, possibly reduced by the search. */
export interface RunRowView {
  run: CheckpointRun
  groups: CheckpointGroup[]
  others: CheckpointFile[]
  /** The search matched files only: the run is shown expanded on them. */
  expand: boolean
}

/** Path of a checkpoint on the machine (``root`` + path relative to it). */
export function hostPath(root: string, rel: string): string {
  return `${root.replace(/\/+$/, "")}/${rel}`
}

/** A checkpoint of a run with its group: ``""`` without ``{group}``, ``null`` outside the layout. */
export interface LabeledFile {
  file: CheckpointFile
  group: string | null
}

/** Every checkpoint of a run in a single list, sorted by name, each with its group. */
export function flatFiles(run: Pick<RunRowView, "groups" | "others">): LabeledFile[] {
  const out: LabeledFile[] = [
    ...run.groups.flatMap((g) => g.files.map((file) => ({ file, group: g.name }))),
    ...run.others.map((file) => ({ file, group: null })),
  ]
  return out.sort((a, b) => a.file.name.localeCompare(b.file.name) || a.file.path.localeCompare(b.file.path))
}

/** Every checkpoint file of a run (groups, then “others”). */
export function runFiles(run: Pick<CheckpointRun, "groups" | "others">): CheckpointFile[] {
  return [...run.groups.flatMap((g) => g.files), ...(run.others ?? [])]
}

/** The search names one of the MLflow runs of the directory (run name or id). */
const mlflowMatches = (run: CheckpointRun, q: string) =>
  (run.mlflow_runs ?? []).some(
    (m) => m.run_id.toLowerCase().startsWith(q) || m.run_name?.toLowerCase().includes(q),
  )

const byDate = (a: CheckpointRun, b: CheckpointRun) =>
  (b.mtime ?? "").localeCompare(a.mtime ?? "") || a.name.localeCompare(b.name)

export function viewRuns(listing: CheckpointListing | undefined, opts: ViewOptions): RunRowView[] {
  if (!listing) return []
  const q = opts.query.trim().toLowerCase()
  const out: RunRowView[] = []
  for (const run of listing.runs) {
    if (opts.hideEmpty && run.count === 0) continue
    if (!q || run.name.toLowerCase().includes(q) || mlflowMatches(run, q)) {
      out.push({ run, groups: run.groups, others: run.others ?? [], expand: false })
      continue
    }
    const match = (f: CheckpointFile) => f.path.toLowerCase().includes(q)
    const groups = run.groups
      .map((g) => ({ ...g, files: g.files.filter(match) }))
      .filter((g) => g.files.length > 0)
    const others = (run.others ?? []).filter(match)
    if (groups.length || others.length) out.push({ run, groups, others, expand: true })
  }
  const cmp =
    opts.sort === "name"
      ? (a: RunRowView, b: RunRowView) => a.run.name.localeCompare(b.run.name)
      : opts.sort === "size"
        ? (a: RunRowView, b: RunRowView) => b.run.size - a.run.size || byDate(a.run, b.run)
        : (a: RunRowView, b: RunRowView) => byDate(a.run, b.run)
  return out.sort(cmp)
}

export type CheckState = "all" | "some" | "none"

/** State of a checkbox covering several files. */
export function checkState(files: CheckpointFile[], selected: ReadonlySet<string>): CheckState {
  let n = 0
  for (const f of files) if (selected.has(f.path)) n++
  return n === 0 ? "none" : n === files.length ? "all" : "some"
}

/** Toggle a set of files: all selected → none, otherwise all. */
export function toggleFiles(files: CheckpointFile[], selected: ReadonlySet<string>): Set<string> {
  const next = new Set(selected)
  const all = checkState(files, selected) === "all"
  for (const f of files) {
    if (all) next.delete(f.path)
    else next.add(f.path)
  }
  return next
}

export interface SelectionSummary {
  files: number
  size: number
  /** Groups whose every checkpoint is selected (the directory goes with them). */
  groups: number
  runs: number
}

export function selectionSummary(
  listing: CheckpointListing | undefined,
  selected: ReadonlySet<string>,
): SelectionSummary {
  const sum: SelectionSummary = { files: 0, size: 0, groups: 0, runs: 0 }
  if (!listing || selected.size === 0) return sum
  const all = [...listing.runs.map((r) => ({ groups: r.groups, others: r.others ?? [] }))]
  all.push({ groups: [], others: listing.others ?? [] })
  for (const r of all) {
    let touched = false
    for (const f of runFiles(r)) {
      if (!selected.has(f.path)) continue
      sum.files++
      sum.size += f.size
      touched = true
    }
    for (const g of r.groups) if (g.files.length && checkState(g.files, selected) === "all") sum.groups++
    if (touched) sum.runs++
  }
  return sum
}

/** Selected paths still present in the listing (after a rescan or a deletion). */
export function pruneSelection(
  listing: CheckpointListing | undefined,
  selected: ReadonlySet<string>,
): Set<string> {
  if (!listing) return new Set()
  const present = new Set<string>()
  for (const r of listing.runs) for (const f of runFiles(r)) present.add(f.path)
  for (const f of listing.others ?? []) present.add(f.path)
  return new Set([...selected].filter((p) => present.has(p)))
}

export interface RunSelectionSummary {
  runs: number
  /** Checkpoints of the selected directories (their other files are not listed). */
  files: number
  size: number
}

/** Selected run directories (whole-directory deletion). */
export function runSelectionSummary(
  listing: CheckpointListing | undefined,
  names: ReadonlySet<string>,
): RunSelectionSummary {
  const sum: RunSelectionSummary = { runs: 0, files: 0, size: 0 }
  for (const r of listing?.runs ?? []) {
    if (!names.has(r.name)) continue
    sum.runs++
    sum.files += r.count
    sum.size += r.size
  }
  return sum
}

/** Selected run directories still present in the listing. */
export function pruneRunSelection(
  listing: CheckpointListing | undefined,
  names: ReadonlySet<string>,
): Set<string> {
  const present = new Set((listing?.runs ?? []).map((r) => r.name))
  return new Set([...names].filter((n) => present.has(n)))
}

// -- inferences ----------------------------------------------------------------------------

export type InferenceState = "running" | "finished" | "failed" | "other"

/** State of an inference: its MLflow run if known, else its TrainHub run. */
export function inferenceState(inf: CheckpointInference): InferenceState {
  const ml = inf.mlflow?.status
  if (ml) {
    if (ml === "RUNNING" || ml === "SCHEDULED") return "running"
    if (ml === "FINISHED") return "finished"
    return "failed"
  }
  switch (inf.status) {
    case "launching":
    case "running":
    case "paused":
    case "stopping":
      return "running"
    case "succeeded":
      return "finished"
    case "failed":
    case "unreachable":
      return "failed"
    default:
      return "other"
  }
}

/** Inferences of a run per checkpoint (path relative to the root), most recent first. */
export function inferencesByFile(run: Pick<CheckpointRun, "inferences">): Map<string, CheckpointInference[]> {
  const out = new Map<string, CheckpointInference[]>()
  for (const inf of run.inferences ?? []) {
    const list = out.get(inf.checkpoint)
    if (list) list.push(inf)
    else out.set(inf.checkpoint, [inf])
  }
  return out
}

/** Checkpoint of an inference as shown under its run: path without the run directory. */
export function inferenceCheckpoint(run: string, inf: CheckpointInference): string {
  return inf.checkpoint.startsWith(`${run}/`) ? inf.checkpoint.slice(run.length + 1) : inf.checkpoint
}

/**
 * A run directory whose training is still going on: its most recent MLflow run (the first
 * one not in the MLflow trash; the listing is most recent first) is running or scheduled.
 */
export function mlflowRunning(
  runs: readonly { status?: MlflowRunRef["status"]; deleted?: boolean }[],
): boolean {
  const latest = runs.find((m) => !m.deleted)
  return latest?.status === "RUNNING" || latest?.status === "SCHEDULED"
}

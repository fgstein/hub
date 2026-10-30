/** Checkpoints tab: filtering, sorting and selection over a source listing (pure functions). */
import type { CheckpointFile, CheckpointGroup, CheckpointListing, CheckpointRun } from "./types"

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

/** Every checkpoint file of a run (groups, then “others”). */
export function runFiles(run: Pick<CheckpointRun, "groups" | "others">): CheckpointFile[] {
  return [...run.groups.flatMap((g) => g.files), ...(run.others ?? [])]
}

const byDate = (a: CheckpointRun, b: CheckpointRun) =>
  (b.mtime ?? "").localeCompare(a.mtime ?? "") || a.name.localeCompare(b.name)

export function viewRuns(listing: CheckpointListing | undefined, opts: ViewOptions): RunRowView[] {
  if (!listing) return []
  const q = opts.query.trim().toLowerCase()
  const out: RunRowView[] = []
  for (const run of listing.runs) {
    if (opts.hideEmpty && run.count === 0) continue
    if (!q || run.name.toLowerCase().includes(q)) {
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

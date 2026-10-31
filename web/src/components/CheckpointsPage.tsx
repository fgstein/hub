import { Tooltip } from "@kobalte/core/tooltip"
import { useNavigate } from "@solidjs/router"
import { createEffect, createMemo, createSignal, For, type JSX, on, onCleanup, onMount, Show } from "solid-js"
import {
  type CheckState,
  checkState,
  flatFiles,
  hostPath,
  type InferenceState,
  inferenceCheckpoint,
  inferenceState,
  inferencesByFile,
  pruneRunSelection,
  pruneSelection,
  type RunRowView,
  runFiles,
  runSelectionSummary,
  type SortKey,
  selectionSummary,
  toggleFiles,
  viewRuns,
} from "~/lib/checkpoints"
import { formatAgo, formatBytes, formatDateTime } from "~/lib/format"
import type {
  CheckpointFile,
  CheckpointInference,
  CheckpointListing,
  CheckpointSourceView,
  MlflowRunRef,
  RunView,
} from "~/lib/types"
import {
  checkpointPick,
  checkpointProjects,
  draftsFromCheckpoints,
  pickCheckpoint,
  project,
  runsByCheckpoint,
  setCheckpointPick,
  state,
} from "~/stores/app"
import {
  deleteCheckpoints,
  deleteRunDirs,
  listings,
  loadCheckpoints,
  loadOrphans,
  orphans,
} from "~/stores/checkpoints"
import { now } from "~/stores/clock"
import { toast } from "~/stores/toasts"
import {
  IconChevronRight,
  IconClose,
  IconCopy,
  IconExternal,
  IconRefresh,
  IconSearch,
  IconSpinner,
  IconTrash,
} from "./Icons"
import { MlflowOrphansView } from "./MlflowOrphans"
import { SlideToConfirm } from "./SlideToConfirm"
import { statusLabel } from "./StatusDot"

const SOURCE_KEY = "trainhub.checkpoints.source"
/** Pseudo source “Orphans”: MLflow runs without a directory (not a valid source id). */
const ORPHANS = "~mlflow"
/** A checkpoint modified less than this long ago may still be being written. */
const WRITING_MS = 60_000

function remembered(): string | null {
  try {
    return localStorage.getItem(SOURCE_KEY)
  } catch {
    return null
  }
}

function remember(id: string): void {
  try {
    localStorage.setItem(SOURCE_KEY, id)
  } catch {
    /* storage unavailable */
  }
}

function Tip(props: { content: JSX.Element; children: JSX.Element; class?: string }) {
  return (
    <Tooltip openDelay={350} placement="top">
      <Tooltip.Trigger as="span" class={props.class}>
        {props.children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content class="z-[90] max-w-md rounded-md bg-float px-2.5 py-1.5 text-xs text-fg shadow-float">
          {props.content}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip>
  )
}

function Check(props: {
  state: CheckState
  onToggle: () => void
  label: string
  danger?: boolean
  testid?: string
}) {
  let el: HTMLInputElement | undefined
  createEffect(() => {
    if (el) el.indeterminate = props.state === "some"
  })
  return (
    <input
      ref={el}
      type="checkbox"
      class="size-3.5 shrink-0 cursor-pointer"
      classList={{ "accent-[var(--text-critical)]": !!props.danger }}
      checked={props.state === "all"}
      aria-label={props.label}
      onClick={(e) => e.stopPropagation()}
      onChange={() => props.onToggle()}
      data-testid={props.testid}
    />
  )
}

/** Size, count and date columns, aligned between runs, groups and files. */
function Cols(props: { count?: JSX.Element; size: number; date?: string | null; dateTitle?: string }) {
  return (
    <>
      <span class="w-24 shrink-0 text-right text-xs tabular text-fg-weak">{props.count}</span>
      <span class="w-20 shrink-0 text-right font-mono text-xs tabular text-fg">
        {formatBytes(props.size)}
      </span>
      <span class="w-24 shrink-0 text-right text-xs tabular text-fg-weak" title={props.dateTitle}>
        {props.date ? formatAgo(props.date, now()) : ""}
      </span>
    </>
  )
}

/** Thin line at the top of a row inside a run, starting at its indent (``before:left-*``). */
const INSET_LINE =
  "relative before:pointer-events-none before:absolute before:top-0 before:right-0 before:h-px before:bg-line-weaker before:content-['']"

const plural = (n: number, word: string, many = `${word}s`) => `${n} ${n === 1 ? word : many}`

/** MLflow column of a run directory: green dot and link if an MLflow run logged from it. */
function MlflowCell(props: { runs: MlflowRunRef[]; match: string }) {
  const latest = () => props.runs[0]
  return (
    <Tip
      class="flex w-16 shrink-0 items-center justify-center gap-1"
      content={
        <Show
          when={latest()}
          fallback={
            <span>
              Orphan: no MLflow run has <code class="font-mono">{props.match}</code> under this directory.
            </span>
          }
        >
          {(ml) => (
            <div class="space-y-0.5">
              <div>
                MLflow run <span class="font-mono text-fg-strong">{ml().run_name ?? ml().run_id}</span>
                {ml().status ? ` · ${ml().status}` : ""}
              </div>
              <Show when={ml().started_at}>
                <div class="text-fg-weak">started {formatDateTime(ml().started_at)}</div>
              </Show>
              <Show when={props.runs.length > 1}>
                <div class="text-fg-weak">
                  {plural(props.runs.length - 1, "earlier MLflow run")} also logged from this directory:{" "}
                  <span class="font-mono">
                    {props.runs
                      .slice(1, 6)
                      .map((m) => m.run_name ?? m.run_id)
                      .join(", ")}
                  </span>
                  {props.runs.length > 6 ? "…" : ""}
                </div>
              </Show>
            </div>
          )}
        </Show>
      }
    >
      <Show
        when={latest()}
        fallback={
          <span
            class="rounded-full border border-current px-1.5 text-[10px] font-medium leading-4 text-fg-warning"
            data-testid="ckpt-mlflow-orphan"
          >
            orphan
          </span>
        }
      >
        <span
          class="dot"
          style={{ "--dot-color": "var(--status-green)" }}
          role="img"
          aria-label="MLflow run found"
          data-testid="ckpt-mlflow-dot"
        />
      </Show>
      <Show when={latest()?.url}>
        <a
          href={latest()!.url!}
          target="_blank"
          rel="noopener noreferrer"
          class="flex size-5 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
          aria-label="Open the run in MLflow"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          data-testid="ckpt-mlflow-link"
        >
          <IconExternal size={11} />
        </a>
      </Show>
    </Tip>
  )
}

const INFERENCE_COLOR: Record<InferenceState, string> = {
  running: "var(--status-green)",
  finished: "var(--status-blue)",
  failed: "var(--status-red)",
  other: "var(--status-grey)",
}

/** Short description of an inference: name, project · config, logged values. */
function inferenceName(inf: CheckpointInference): string {
  return inf.mlflow?.run_name ?? inf.trainhub_run ?? inf.mlflow?.run_id ?? "?"
}

/** Inference count of a run or a checkpoint, with the list in its tooltip. */
function InferenceBadge(props: {
  inferences: CheckpointInference[]
  run: string
  testid: string
  active?: boolean
  onClick?: () => void
}) {
  return (
    <Tip
      class="shrink-0"
      content={
        <div class="space-y-0.5">
          <div>
            {plural(props.inferences.length, "inference")} with{" "}
            {props.testid === "ckpt-file-inferences" ? "this checkpoint" : "the checkpoints of this run"}:
          </div>
          <For each={props.inferences.slice(0, 8)}>
            {(inf) => (
              <div class="flex items-center gap-1.5 font-mono">
                <span class="dot" style={{ "--dot-color": INFERENCE_COLOR[inferenceState(inf)] }} />
                {inferenceName(inf)}
                <span class="text-fg-weak">
                  {inf.config ? ` · ${inf.config}` : ""}
                  {props.testid === "ckpt-file-inferences" ? "" : ` · ${inferenceCheckpoint(props.run, inf)}`}
                  {inf.started_at ? ` · ${formatAgo(inf.started_at, now())}` : ""}
                </span>
              </div>
            )}
          </For>
          <Show when={props.inferences.length > 8}>
            <div>…</div>
          </Show>
        </div>
      }
    >
      <button
        type="button"
        class="flex h-5 items-center rounded px-1.5 font-mono text-xs hover:bg-surface-hover hover:text-fg-strong"
        classList={{
          "bg-surface text-fg-weak": !props.active,
          "bg-surface-active text-fg-strong": !!props.active,
        }}
        aria-expanded={props.active}
        onClick={(e) => {
          e.stopPropagation()
          props.onClick?.()
        }}
        onKeyDown={(e) => e.stopPropagation()}
        aria-label={plural(props.inferences.length, "inference")}
        data-testid={props.testid}
      >
        ⇢ {props.inferences.length}
      </button>
    </Tip>
  )
}

/** Toolbar summary of the MLflow link of a source: directories linked, errors. */
function MlflowSummary(props: { listing: CheckpointListing }) {
  const ml = () => props.listing.mlflow!
  const linked = () => props.listing.runs.filter((r) => r.mlflow_runs?.length).length
  return (
    <Tip
      class="shrink-0 text-xs"
      content={
        <div class="space-y-0.5">
          <div>
            Run directories linked to the MLflow run that logged their path in{" "}
            <code class="font-mono">{ml().match}</code> ({ml().tracking_uri}).
          </div>
          <div class="text-fg-weak">
            {plural(ml().searched, "MLflow run")} have this key
            {ml().searched_at ? ` — searched ${formatDateTime(ml().searched_at)}` : ""}, cached with the scan.
          </div>
          <Show when={ml().truncated}>
            <div class="text-fg-warning">
              Search stopped early: only the most recent MLflow runs are linked.
            </div>
          </Show>
          <Show when={ml().error}>
            <div class="text-fg-critical">{ml().error}</div>
          </Show>
        </div>
      }
    >
      <span
        classList={{ "text-fg-weaker": !ml().error, "text-fg-warning": !!ml().error }}
        data-testid="ckpt-mlflow-summary"
      >
        · MLflow {linked()}/{props.listing.runs.length}
        {ml().error ? " ⚠" : ""}
      </span>
    </Tip>
  )
}

export function CheckpointsPage(props: { source?: string }) {
  const navigate = useNavigate()
  const sources = () => state.inventory?.checkpoint_sources ?? []
  const mlflowEnabled = () => sources().some((s) => s.mlflow_match)
  const sourceId = createMemo(() => {
    const ids = sources().map((s) => s.id)
    const wanted = props.source ?? remembered()
    if (wanted === ORPHANS && mlflowEnabled()) return ORPHANS
    return wanted && ids.includes(wanted) ? wanted : (ids[0] ?? null)
  })
  const source = () => sources().find((s) => s.id === sourceId())
  const entry = () => (sourceId() ? listings[sourceId()!] : undefined)
  const listing = () => entry()?.data

  const [query, setQuery] = createSignal("")
  const [sort, setSort] = createSignal<SortKey>("date")
  const [hideEmpty, setHideEmpty] = createSignal(false)
  const [expanded, setExpanded] = createSignal<Set<string>>(new Set())
  const [deleteMode, setDeleteMode] = createSignal(false)
  const [toDelete, setToDelete] = createSignal<Set<string>>(new Set())
  /** What the delete mode removes: checkpoint files, or whole run directories. */
  const [deleteScope, setDeleteScope] = createSignal<"files" | "runs">("files")
  const [runsToDelete, setRunsToDelete] = createSignal<Set<string>>(new Set())
  const deletingRuns = () => deleteMode() && deleteScope() === "runs"
  const [toSend, setToSend] = createSignal<Set<string>>(new Set())
  const [deleting, setDeleting] = createSignal(false)
  const [target, setTarget] = createSignal<string | null>(null)
  /** Checkpoints whose inferences are shown under them (hidden by default). */
  const [openInf, setOpenInf] = createSignal<Set<string>>(new Set())
  const toggleInf = (path: string) =>
    setOpenInf((s) => {
      const n = new Set(s)
      if (n.has(path)) n.delete(path)
      else n.add(path)
      return n
    })

  // Every source is loaded (the server caches the scans): their totals show in the header.
  createEffect(
    on(
      () => sources().map((s) => s.id),
      (ids) => {
        for (const id of ids) void loadCheckpoints(id)
      },
    ),
  )
  // The MLflow runs without a directory: their count shows on their button.
  createEffect(() => {
    if (mlflowEnabled()) void loadOrphans()
  })
  createEffect(
    on(sourceId, (id) => {
      if (!id) return
      remember(id)
      setExpanded(new Set<string>())
      setOpenInf(new Set<string>())
      setToDelete(new Set<string>())
      setRunsToDelete(new Set<string>())
      setToSend(new Set<string>())
      setDeleteMode(false)
    }),
  )
  // Selections follow the listing (rescan, deletion from another client).
  createEffect(
    on(listing, (l) => {
      setToDelete((s) => pruneSelection(l, s))
      setRunsToDelete((s) => pruneRunSelection(l, s))
      setToSend((s) => pruneSelection(l, s))
    }),
  )

  const rows = createMemo(() => viewRuns(listing(), { query: query(), sort: sort(), hideEmpty: hideEmpty() }))
  const byCheckpoint = createMemo(runsByCheckpoint)
  /** Inferences per checkpoint of the listing (path relative to the root). */
  const inferencesByPath = createMemo(() => {
    const out = new Map<string, CheckpointInference[]>()
    for (const r of listing()?.runs ?? []) for (const [k, v] of inferencesByFile(r)) out.set(k, v)
    return out
  })
  /** Label column (group / other) when some checkpoint of the listing has one. */
  const hasLabels = () =>
    (listing()?.runs ?? []).some((r) => r.groups.some((g) => g.name) || r.others?.length)
  const showInferences = () =>
    !!listing()?.mlflow_inference || (listing()?.runs ?? []).some((r) => r.inferences?.length)
  const targets = createMemo(checkpointProjects)
  const targetId = () => {
    const t = target()
    const list = targets()
    return t && list.some((p) => p.id === t) ? t : (list[0]?.id ?? null)
  }
  const deleteSummary = createMemo(() => selectionSummary(listing(), toDelete()))
  const runDeleteSummary = createMemo(() => runSelectionSummary(listing(), runsToDelete()))
  const toggleRunDir = (name: string) =>
    setRunsToDelete((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  const sendSummary = createMemo(() => selectionSummary(listing(), toSend()))

  const isOpen = (r: RunRowView) => r.expand || expanded().has(r.run.name)
  const toggleRun = (name: string) =>
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  const allOpen = () => rows().length > 0 && rows().every(isOpen)

  const enterDelete = () => {
    setToSend(new Set<string>())
    setToDelete(new Set<string>())
    setRunsToDelete(new Set<string>())
    setDeleteScope("files")
    setDeleteMode(true)
  }
  const exitDelete = () => {
    setDeleteMode(false)
    setToDelete(new Set<string>())
    setRunsToDelete(new Set<string>())
  }
  const clearDeleteSelection = () => {
    setToDelete(new Set<string>())
    setRunsToDelete(new Set<string>())
  }

  onMount(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return
      if (picking()) {
        e.preventDefault()
        cancelPick()
      } else if (deleteMode()) {
        e.preventDefault()
        exitDelete()
      } else if (toSend().size) {
        e.preventDefault()
        setToSend(new Set<string>())
      }
    }
    window.addEventListener("keydown", onKey)
    onCleanup(() => window.removeEventListener("keydown", onKey))
  })
  // Leaving the tab without choosing cancels the pick.
  onCleanup(() => setCheckpointPick(null))

  const confirmDeleteRuns = async () => {
    const src = sourceId()
    const names = [...runsToDelete()]
    if (!src || !names.length || deleting()) return
    setDeleting(true)
    const res = await deleteRunDirs(src, names)
    setDeleting(false)
    if (!res) return
    const failed = Object.entries(res.failed ?? {})
    if (res.deleted.length) {
      toast(
        `${plural(res.deleted.length, "run directory", "run directories")} deleted, ${formatBytes(res.freed)} of checkpoints freed`,
        "success",
      )
    }
    if (failed.length) {
      const [name, err] = failed[0]!
      toast(
        `${plural(failed.length, "run directory", "run directories")} not deleted: ${name}: ${err}${failed.length > 1 ? " (…)" : ""}`,
        "error",
        10000,
      )
      setRunsToDelete(new Set(failed.map(([n]) => n)))
    } else {
      exitDelete()
    }
  }

  const confirmDelete = async () => {
    if (deleteScope() === "runs") return confirmDeleteRuns()
    const src = sourceId()
    const paths = [...toDelete()]
    if (!src || !paths.length || deleting()) return
    setDeleting(true)
    const res = await deleteCheckpoints(src, paths)
    setDeleting(false)
    if (!res) return
    const failed = Object.entries(res.failed ?? {})
    if (res.deleted.length) {
      toast(`${plural(res.deleted.length, "checkpoint")} deleted, ${formatBytes(res.freed)} freed`, "success")
    }
    if (failed.length) {
      const [path, err] = failed[0]!
      toast(
        `${plural(failed.length, "checkpoint")} not deleted: ${path}: ${err}${failed.length > 1 ? " (…)" : ""}`,
        "error",
        10000,
      )
      // the failed ones stay selected
      setToDelete(new Set(failed.map(([p]) => p)))
    } else {
      exitDelete()
    }
  }

  // -- draft → checkpoint: the clicked checkpoint fills the waiting draft ----------------

  const picking = () => (deleteMode() ? null : checkpointPick())
  const pickingLabel = () => {
    const pid = checkpointPick()?.projectId
    return project(pid)?.label ?? pid
  }
  const backToDraft = (pid: string) => navigate(`/p/${encodeURIComponent(pid)}`)
  const cancelPick = () => {
    const pid = checkpointPick()?.projectId
    setCheckpointPick(null)
    if (pid) backToDraft(pid)
  }
  const pickFile = (file: CheckpointFile) => {
    const src = source()
    if (!src) return
    const pid = pickCheckpoint(src.machine ?? null, hostPath(src.root, file.path))
    if (pid) backToDraft(pid)
    else toast("the draft no longer exists", "error")
  }

  const sendTo = () => {
    const src = source()
    const pid = targetId()
    const paths = [...toSend()]
    if (!src || !pid || !paths.length) return
    const n = draftsFromCheckpoints(
      pid,
      src.machine,
      paths.map((p) => hostPath(src.root, p)),
    )
    setToSend(new Set<string>())
    navigate(`/p/${encodeURIComponent(pid)}`)
    toast(`${plural(n, "draft")} created in ${targets().find((p) => p.id === pid)?.label ?? pid}`, "success")
  }

  const copyPaths = async () => {
    const src = source()
    if (!src) return
    const text = [...toSend()].map((p) => hostPath(src.root, p)).join("\n")
    try {
      await navigator.clipboard.writeText(text)
      toast(`${plural(toSend().size, "path")} copied`, "success")
    } catch {
      toast("clipboard unavailable", "error")
    }
  }

  // -- rows --------------------------------------------------------------------------------

  /** A checkpoint line; ``group``: label before its name (``null``: outside the layout). */
  const FileRow = (fp: {
    file: CheckpointFile
    indent: string
    deletable: boolean
    group?: string | null
    labels?: boolean
  }) => {
    const path = () => (source() ? hostPath(source()!.root, fp.file.path) : fp.file.path)
    const runs = (): RunView[] => byCheckpoint()[path()] ?? []
    const selected = () => (deleteMode() ? toDelete() : toSend()).has(fp.file.path)
    const deletable = () => fp.deletable && !deletingRuns()
    const toggle = () => {
      if (picking()) pickFile(fp.file)
      else if (deleteMode()) {
        if (deletable()) setToDelete((s) => toggleFiles([fp.file], s))
      } else setToSend((s) => toggleFiles([fp.file], s))
    }
    const writing = () => now() - Date.parse(fp.file.mtime) < WRITING_MS
    const infs = () => inferencesByPath().get(fp.file.path)
    return (
      <>
        <div
          class={`${INSET_LINE} before:left-10 flex h-8 cursor-pointer items-center gap-2 pr-4 hover:bg-surface-hover ${fp.indent}`}
          classList={{
            "bg-surface-active": selected() && !deleteMode(),
            "bg-critical-bg": selected() && deleteMode(),
            "opacity-50": deleteMode() && !fp.deletable,
            "cursor-default": deletingRuns(),
          }}
          onClick={toggle}
          data-testid="ckpt-file"
          data-path={fp.file.path}
        >
          <Show
            when={!picking() && (!deleteMode() || deletable())}
            fallback={
              <span
                class="size-3.5 shrink-0"
                title={
                  picking() || deletingRuns()
                    ? undefined
                    : "Not under a run directory: cannot be deleted here"
                }
              />
            }
          >
            <Check
              state={selected() ? "all" : "none"}
              onToggle={toggle}
              label={`Select ${fp.file.name}`}
              danger={deleteMode()}
              testid="ckpt-file-check"
            />
          </Show>
          <Show when={fp.labels}>
            <span class="w-12 shrink-0">
              <Show when={fp.group !== ""}>
                <span
                  class="inline-block max-w-full truncate rounded bg-surface px-1.5 align-middle font-mono text-[10px] leading-4"
                  classList={{ "text-fg-weak": fp.group != null, "text-fg-weaker italic": fp.group == null }}
                  title={fp.group == null ? "Outside the layout" : `Group ${fp.group}`}
                  data-testid="ckpt-file-group"
                >
                  {fp.group ?? "other"}
                </span>
              </Show>
            </span>
          </Show>
          <span class="min-w-0 flex-1 truncate font-mono text-sm text-fg-strong" title={path()}>
            {fp.file.name}
          </span>
          <Show when={writing()}>
            <Tip
              class="shrink-0 text-xs text-fg-warning"
              content="Modified less than a minute ago: maybe still being written"
            >
              writing?
            </Tip>
          </Show>
          <Show when={infs()}>
            {(list) => (
              <InferenceBadge
                inferences={list()}
                run=""
                testid="ckpt-file-inferences"
                active={openInf().has(fp.file.path)}
                onClick={() => toggleInf(fp.file.path)}
              />
            )}
          </Show>
          <Show when={runs().length}>
            <Tip
              class="shrink-0"
              content={
                <div class="space-y-0.5 font-mono">
                  <For each={runs().slice(0, 8)}>
                    {(r) => (
                      <div>
                        {r.id} · {r.project_id} · {statusLabel(r.status)} · {formatDateTime(r.created_at)}
                      </div>
                    )}
                  </For>
                  <Show when={runs().length > 8}>
                    <div>…</div>
                  </Show>
                </div>
              }
            >
              <button
                type="button"
                class="flex h-5 items-center rounded bg-surface px-1.5 font-mono text-xs text-fg-weak hover:bg-surface-hover hover:text-fg-strong"
                onClick={(e) => {
                  e.stopPropagation()
                  const r = runs()[0]
                  if (r) navigate(`/p/${encodeURIComponent(r.project_id)}`)
                }}
                aria-label={`${plural(runs().length, "run")} launched with this checkpoint`}
                data-testid="ckpt-file-runs"
              >
                ⟲ {runs().length}
              </button>
            </Tip>
          </Show>
          <Cols size={fp.file.size} date={fp.file.mtime} dateTitle={formatDateTime(fp.file.mtime)} />
        </div>
        <Show when={openInf().has(fp.file.path) && infs()}>
          {(list) => (
            <For each={list()}>
              {(inf) => <InferenceRow run="" inf={inf} indent="pl-20" showCheckpoint={false} />}
            </For>
          )}
        </Show>
      </>
    )
  }

  /** Inferences made with checkpoints of a run deleted since (the others are under their file). */
  const InferenceList = (ip: { run: string; inferences: CheckpointInference[] }) => {
    const [open, setOpen] = createSignal(false)
    return (
      <div data-testid="ckpt-inferences">
        <div
          class={`${INSET_LINE} before:left-10 flex h-8 cursor-pointer items-center gap-2 pr-4 pl-10 hover:bg-surface-hover`}
          role="button"
          tabIndex={0}
          aria-expanded={open()}
          onClick={() => setOpen(!open())}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault()
              setOpen(!open())
            }
          }}
        >
          <IconChevronRight
            size={11}
            class={`shrink-0 text-icon transition-transform duration-150 ${open() ? "rotate-90" : ""}`}
          />
          <span class="min-w-0 flex-1 truncate text-sm text-fg">inferences on deleted checkpoints</span>
          <span class="w-24 shrink-0 text-right text-xs tabular text-fg-weak">
            {plural(ip.inferences.length, "run")}
          </span>
          <span class="w-20 shrink-0" />
          <span class="w-24 shrink-0" />
        </div>
        <Show when={open()}>
          <For each={ip.inferences}>
            {(inf) => <InferenceRow run={ip.run} inf={inf} indent="pl-16" showCheckpoint={true} />}
          </For>
        </Show>
      </div>
    )
  }

  const InferenceRow = (rp: {
    run: string
    inf: CheckpointInference
    indent: string
    showCheckpoint: boolean
  }) => {
    const inf = () => rp.inf
    const ckpt = () => inferenceCheckpoint(rp.run, inf())
    const state = () => inferenceState(inf())
    return (
      <div
        class={`${INSET_LINE} before:left-16 flex h-8 items-center gap-2 bg-surface pr-4 hover:bg-surface-hover ${rp.indent}`}
        data-testid="ckpt-inference"
      >
        <span
          class="dot"
          style={{ "--dot-color": INFERENCE_COLOR[state()] }}
          role="img"
          aria-label={inf().mlflow?.status ?? inf().status ?? "unknown status"}
          title={[
            inf().mlflow?.status && `MLflow: ${inf().mlflow!.status}`,
            inf().status && `TrainHub: ${inf().status}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        />
        <span class="w-40 shrink-0 truncate font-mono text-sm text-fg-strong" title={inferenceName(inf())}>
          {inferenceName(inf())}
        </span>
        <span
          class="min-w-0 flex-1 truncate font-mono text-xs"
          classList={{ "text-fg": inf().exists, "text-fg-weaker line-through": !inf().exists }}
          title={inf().exists ? inf().checkpoint : `${inf().checkpoint} (deleted since)`}
          data-testid="ckpt-inference-checkpoint"
        >
          {rp.showCheckpoint ? ckpt() : ""}
        </span>
        <Show when={!inf().exists && rp.showCheckpoint}>
          <span class="shrink-0 text-xs text-fg-weaker">deleted</span>
        </Show>
        <Show when={inf().config}>
          <span
            class="shrink-0 rounded bg-surface px-1.5 font-mono text-xs text-fg"
            title={`TrainHub: project ${inf().project}, config ${inf().config}`}
          >
            {inf().config}
          </span>
        </Show>
        <For each={Object.entries(inf().logged ?? {})}>
          {([k, v]) => (
            <span
              class="max-w-40 shrink-0 truncate rounded bg-surface px-1.5 font-mono text-xs text-fg-weak"
              title={`${k} = ${v}`}
            >
              {v}
            </span>
          )}
        </For>
        <span class="flex w-12 shrink-0 justify-center gap-0.5">
          <Show when={inf().trainhub_run && inf().project}>
            <button
              type="button"
              class="flex size-5 items-center justify-center rounded font-mono text-xs text-icon hover:bg-surface-hover hover:text-icon-hover"
              title={`TrainHub run ${inf().trainhub_run} (${inf().project})`}
              aria-label="Open the TrainHub project"
              onClick={() => navigate(`/p/${encodeURIComponent(inf().project!)}`)}
              data-testid="ckpt-inference-trainhub"
            >
              ⟲
            </button>
          </Show>
          <Show when={inf().mlflow?.url}>
            <a
              href={inf().mlflow!.url!}
              target="_blank"
              rel="noopener noreferrer"
              class="flex size-5 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
              aria-label="Open the inference in MLflow"
              data-testid="ckpt-inference-mlflow"
            >
              <IconExternal size={11} />
            </a>
          </Show>
        </span>
        <span
          class="w-24 shrink-0 text-right text-xs tabular text-fg-weak"
          title={formatDateTime(inf().started_at)}
        >
          {inf().started_at ? formatAgo(inf().started_at, now()) : ""}
        </span>
      </div>
    )
  }

  const RunBlock = (rp: { row: RunRowView }) => {
    const run = () => rp.row.run
    const open = () => isOpen(rp.row)
    const filesSelected = () => {
      if (deletingRuns()) return 0
      const sel = deleteMode() ? toDelete() : toSend()
      return runFiles(rp.row).filter((f) => sel.has(f.path)).length
    }
    const dirSelected = () => deletingRuns() && runsToDelete().has(run().name)
    return (
      <div class="border-b border-line-weak" data-testid="ckpt-run" data-run={run().name}>
        <div
          class="flex h-10 cursor-pointer items-center gap-2 px-4 outline-none hover:bg-surface-hover focus-visible:bg-surface-hover"
          classList={{ "bg-critical-bg": dirSelected() }}
          role="button"
          tabIndex={0}
          aria-expanded={open()}
          onClick={() => toggleRun(run().name)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault()
              toggleRun(run().name)
            }
          }}
        >
          <IconChevronRight
            size={11}
            class={`shrink-0 text-icon transition-transform duration-150 ${open() ? "rotate-90" : ""}`}
          />
          <Show when={deletingRuns()}>
            <Check
              state={dirSelected() ? "all" : "none"}
              onToggle={() => toggleRunDir(run().name)}
              label={`Select the whole directory ${run().name}`}
              danger
              testid="ckpt-rundir-check"
            />
          </Show>
          <Show when={deleteMode() && !deletingRuns() && runFiles(rp.row).length}>
            <Check
              state={checkState(runFiles(rp.row), toDelete())}
              onToggle={() => setToDelete((s) => toggleFiles(runFiles(rp.row), s))}
              label={`Select every checkpoint of ${run().name}`}
              danger
              testid="ckpt-run-check"
            />
          </Show>
          <span
            class="min-w-0 flex-1 truncate font-mono text-sm font-medium text-fg-strong"
            title={run().name}
          >
            {run().name}
          </span>
          <Show when={filesSelected()}>
            <span
              class="shrink-0 rounded px-1.5 text-xs tabular"
              classList={{
                "bg-critical-bg text-fg-critical": deleteMode(),
                "bg-surface-active text-fg-strong": !deleteMode(),
              }}
            >
              {filesSelected()} selected
            </span>
          </Show>
          <Show when={dirSelected()}>
            <span class="shrink-0 rounded bg-critical-bg px-1.5 text-xs text-fg-critical">
              whole directory
            </span>
          </Show>
          <Show when={listing()?.mlflow}>
            {(ml) => <MlflowCell runs={run().mlflow_runs ?? []} match={ml().match} />}
          </Show>
          <Show when={showInferences()}>
            <span class="flex w-12 shrink-0 justify-center">
              <Show when={run().inferences?.length}>
                <InferenceBadge
                  inferences={run().inferences ?? []}
                  run={run().name}
                  testid="ckpt-run-inferences"
                  onClick={() => {
                    if (!open()) toggleRun(run().name)
                    const paths = (run().inferences ?? []).filter((i) => i.exists).map((i) => i.checkpoint)
                    setOpenInf((s) => new Set([...s, ...paths]))
                  }}
                />
              </Show>
            </span>
          </Show>
          <Cols
            count={
              run().count ? plural(run().count, "ckpt") : <span class="text-fg-weaker">no checkpoint</span>
            }
            size={run().size}
            date={run().mtime}
            dateTitle={formatDateTime(run().mtime)}
          />
        </div>
        <Show when={open()}>
          <div class="pb-1">
            <For each={flatFiles(rp.row)}>
              {(x) => (
                <FileRow file={x.file} group={x.group} labels={hasLabels()} indent="pl-10" deletable={true} />
              )}
            </For>
            <Show when={(run().inferences ?? []).some((i) => !i.exists)}>
              <InferenceList
                run={run().name}
                inferences={(run().inferences ?? []).filter((i) => !i.exists)}
              />
            </Show>
            <Show when={run().count === 0}>
              <div class="flex h-8 items-center pl-10 text-xs text-fg-weaker">No checkpoint in this run.</div>
            </Show>
          </div>
        </Show>
      </div>
    )
  }

  // -- page --------------------------------------------------------------------------------

  const SourceButton = (sp: { src: CheckpointSourceView }) => {
    const active = () => sp.src.id === sourceId()
    const loading = () => listings[sp.src.id]?.loading
    return (
      <button
        type="button"
        class="flex h-12 shrink-0 flex-col items-start justify-center rounded-md border px-3 text-left transition-colors"
        classList={{
          "border-line bg-surface-active": active(),
          "border-line-weaker hover:border-line-weak hover:bg-surface-hover": !active(),
        }}
        onClick={() => navigate(`/checkpoints/${encodeURIComponent(sp.src.id)}`)}
        data-testid={`ckpt-source-${sp.src.id}`}
        aria-pressed={active()}
      >
        <span class="text-sm font-medium text-fg-strong">{sp.src.label}</span>
        <span class="flex items-center gap-1 text-xs tabular text-fg-weak" data-testid="ckpt-source-total">
          <Show when={sp.src.size != null} fallback={loading() ? "scanning…" : "not scanned"}>
            <span class="font-mono text-fg">{formatBytes(sp.src.size)}</span>
            <span>· {plural(sp.src.count ?? 0, "checkpoint")}</span>
          </Show>
          <Show when={loading() && sp.src.size != null}>
            <IconSpinner size={10} />
          </Show>
        </span>
      </button>
    )
  }

  const OrphansButton = () => {
    const active = () => sourceId() === ORPHANS
    const count = () => orphans.data?.runs.length
    return (
      <button
        type="button"
        class="flex h-12 shrink-0 flex-col items-start justify-center rounded-md border border-dashed px-3 text-left transition-colors"
        classList={{
          "border-line bg-surface-active": active(),
          "border-line-weak hover:bg-surface-hover": !active(),
        }}
        onClick={() => navigate(`/checkpoints/${encodeURIComponent(ORPHANS)}`)}
        title="Orphans: MLflow runs whose logged directory is in no checkpoint source"
        data-testid="ckpt-source-mlflow-orphans"
        aria-pressed={active()}
      >
        <span class="text-sm font-medium text-fg-strong">Orphans</span>
        <span class="flex items-center gap-1 text-xs tabular text-fg-weak">
          <Show when={count() != null} fallback={orphans.loading ? "searching…" : "not searched"}>
            <span classList={{ "text-fg-warning": !!count() }}>{plural(count() ?? 0, "MLflow run")}</span>
          </Show>
          <Show when={orphans.loading && count() != null}>
            <IconSpinner size={10} />
          </Show>
        </span>
      </button>
    )
  }

  return (
    <div class="flex min-h-0 flex-1 flex-col" data-testid="checkpoints-page">
      <Show
        when={sources().length}
        fallback={
          <div class="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-sm text-fg-weak">
            <p>No checkpoint source in the inventory.</p>
            <p class="max-w-md text-xs">
              Add a <code class="font-mono">checkpoints:</code> section (machine, root, layout) to list the
              runs already executed and their checkpoints.
            </p>
          </div>
        }
      >
        <div class="flex shrink-0 gap-2 overflow-x-auto border-b border-line-weak px-4 py-2.5">
          <For each={sources()}>{(src) => <SourceButton src={src} />}</For>
          <Show when={mlflowEnabled()}>
            <OrphansButton />
          </Show>
        </div>
        <Show when={sourceId() !== ORPHANS} fallback={<MlflowOrphansView />}>
          <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-line-weaker px-4 py-2">
            <span class="min-w-0 truncate font-mono text-xs text-fg-weak" title={source()?.layout}>
              {source()?.machine ?? "TrainHub server"}:{source()?.root}
            </span>
            <Tip
              class="shrink-0 text-xs text-fg-weaker"
              content={`Scanned ${formatDateTime(listing()?.scanned_at)} — cached 5 min on the server`}
            >
              <Show when={listing()?.scanned_at}>· scanned {formatAgo(listing()?.scanned_at, now())}</Show>
            </Tip>
            <Show when={listing()?.mlflow && listing()}>{(l) => <MlflowSummary listing={l()} />}</Show>
            <button
              type="button"
              class="flex size-6 shrink-0 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover disabled:opacity-40"
              title="Rescan the disk"
              aria-label="Rescan the disk"
              disabled={entry()?.loading}
              onClick={() => sourceId() && void loadCheckpoints(sourceId()!, "rescan")}
              data-testid="ckpt-refresh"
            >
              <Show when={entry()?.loading} fallback={<IconRefresh size={12} />}>
                <IconSpinner size={12} />
              </Show>
            </button>
            <div class="flex-1" />
            <label class="flex h-7 w-56 items-center gap-1.5 rounded border border-line-weak bg-input px-2 focus-within:border-line">
              <IconSearch size={12} class="shrink-0 text-icon" />
              <input
                value={query()}
                onInput={(e) => setQuery(e.currentTarget.value)}
                placeholder="Search runs, checkpoints…"
                aria-label="Search runs and checkpoints"
                spellcheck={false}
                class="h-full min-w-0 flex-1 bg-transparent text-sm text-fg-strong outline-none placeholder:text-fg-weaker"
                data-testid="ckpt-search"
              />
            </label>
            <div
              class="flex h-7 items-center rounded border border-line-weak p-0.5 text-xs"
              role="group"
              aria-label="Sort"
            >
              <For each={["date", "name", "size"] as SortKey[]}>
                {(k) => (
                  <button
                    type="button"
                    class="h-full rounded px-2"
                    classList={{
                      "bg-surface-active text-fg-strong": sort() === k,
                      "text-fg-weak hover:text-fg-strong": sort() !== k,
                    }}
                    onClick={() => setSort(k)}
                    aria-pressed={sort() === k}
                  >
                    {k}
                  </button>
                )}
              </For>
            </div>
            <label class="flex items-center gap-1.5 text-xs text-fg-weak">
              <input
                type="checkbox"
                class="size-3.5"
                checked={hideEmpty()}
                onChange={(e) => setHideEmpty(e.currentTarget.checked)}
              />
              hide empty
            </label>
            <button
              type="button"
              class="h-7 rounded px-2 text-xs text-fg-weak hover:bg-surface-hover hover:text-fg-strong"
              onClick={() =>
                setExpanded(allOpen() ? new Set<string>() : new Set(rows().map((r) => r.run.name)))
              }
            >
              {allOpen() ? "collapse all" : "expand all"}
            </button>
            <Show
              when={deleteMode()}
              fallback={
                <button
                  type="button"
                  class="flex h-7 items-center gap-1.5 rounded border border-line-weak px-2 text-xs text-fg hover:border-line-critical hover:text-fg-critical"
                  onClick={enterDelete}
                  disabled={!listing()?.count}
                  data-testid="ckpt-delete-mode"
                >
                  <IconTrash size={12} /> Delete…
                </button>
              }
            >
              <button
                type="button"
                class="flex h-7 items-center gap-1.5 rounded bg-critical-bg px-2 text-xs text-fg-critical"
                onClick={exitDelete}
                data-testid="ckpt-delete-mode"
              >
                <IconClose size={10} /> Exit delete mode
              </button>
            </Show>
          </div>
          <Show when={picking()}>
            <div
              class="flex shrink-0 items-center gap-2 border-b border-line-weak bg-surface-active px-4 py-1.5 text-xs text-fg-strong"
              data-testid="ckpt-pick-bar"
            >
              Click a checkpoint to use it in the draft of <span class="font-medium">{pickingLabel()}</span>
              <div class="flex-1" />
              <button
                type="button"
                class="h-6 rounded px-2 text-fg-weak hover:bg-surface-hover hover:text-fg-strong"
                onClick={cancelPick}
              >
                Cancel
              </button>
            </div>
          </Show>
          <Show when={entry()?.error}>
            <div class="shrink-0 border-b border-line-weaker bg-critical-bg px-4 py-1.5 text-xs text-fg-critical">
              {entry()?.error}
            </div>
          </Show>
          <div class="min-h-0 flex-1 overflow-y-auto" data-testid="ckpt-list">
            <Show
              when={listing()}
              fallback={
                <div class="flex items-center gap-2 px-4 py-6 text-sm text-fg-weak">
                  <Show when={entry()?.loading}>
                    <IconSpinner size={12} /> Scanning {source()?.root}…
                  </Show>
                </div>
              }
            >
              <Show when={deleteMode()}>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-weaker px-4 py-1.5 text-xs text-fg-critical">
                  <div
                    class="flex h-6 items-center rounded border border-line-critical p-0.5"
                    role="group"
                    aria-label="What to delete"
                  >
                    <For
                      each={
                        [
                          ["files", "Checkpoints"],
                          ["runs", "Whole run directories"],
                        ] as const
                      }
                    >
                      {([k, label]) => (
                        <button
                          type="button"
                          class="h-full rounded px-2"
                          classList={{
                            "bg-critical-bg text-fg-critical": deleteScope() === k,
                            "text-fg-weak hover:text-fg-strong": deleteScope() !== k,
                          }}
                          onClick={() => {
                            if (deleteScope() === k) return
                            clearDeleteSelection()
                            setDeleteScope(k)
                          }}
                          aria-pressed={deleteScope() === k}
                          data-testid={`ckpt-delete-scope-${k}`}
                        >
                          {label}
                        </button>
                      )}
                    </For>
                  </div>
                  <span>
                    <Show
                      when={deletingRuns()}
                      fallback="Tick checkpoints or all those of a run (the run directory stays), then slide to confirm at the bottom."
                    >
                      Tick runs: their whole directory is removed (checkpoints, logs, configs, everything in
                      it), then slide to confirm at the bottom.
                    </Show>
                  </span>
                </div>
              </Show>
              <For
                each={rows()}
                fallback={
                  <div class="px-4 py-6 text-sm text-fg-weak">
                    {query() ? "Nothing matches the search." : "No run directory under the root."}
                  </div>
                }
              >
                {(row) => <RunBlock row={row} />}
              </For>
              <Show when={!deleteMode() && (listing()?.others?.length ?? 0) > 0 && !query()}>
                <div class="border-b border-line-weaker">
                  <div class="flex h-10 items-center px-4 text-xs text-fg-weak">
                    Checkpoints directly under the root (no run directory)
                  </div>
                  <For each={listing()?.others ?? []}>
                    {(f) => <FileRow file={f} indent="pl-10" deletable={false} />}
                  </For>
                </div>
              </Show>
            </Show>
          </div>
          <Show when={deleteMode()}>
            <div
              class="flex shrink-0 flex-wrap items-center gap-3 border-t border-line-critical bg-bg-stronger px-4 py-2.5"
              data-testid="ckpt-delete-bar"
            >
              <span class="text-sm text-fg-strong" data-testid="ckpt-delete-summary">
                <Show
                  when={!deletingRuns()}
                  fallback={
                    <Show
                      when={runDeleteSummary().runs}
                      fallback={<span class="text-fg-weak">Nothing selected</span>}
                    >
                      {plural(runDeleteSummary().runs, "run directory", "run directories")} (
                      {plural(runDeleteSummary().files, "checkpoint")} ·{" "}
                      <span class="font-mono font-medium text-fg-critical">
                        {formatBytes(runDeleteSummary().size)}
                      </span>
                      , plus their other files)
                    </Show>
                  }
                >
                  <Show
                    when={deleteSummary().files}
                    fallback={<span class="text-fg-weak">Nothing selected</span>}
                  >
                    <Show when={deleteSummary().groups}>{plural(deleteSummary().groups, "group")}, </Show>
                    {plural(deleteSummary().files, "checkpoint")} in {plural(deleteSummary().runs, "run")} ·{" "}
                    <span class="font-mono font-medium text-fg-critical">
                      {formatBytes(deleteSummary().size)}
                    </span>
                  </Show>
                </Show>
              </span>
              <div class="flex-1" />
              <button
                type="button"
                class="h-7 rounded px-2 text-xs text-fg-weak hover:text-fg-strong"
                onClick={clearDeleteSelection}
                disabled={!toDelete().size && !runsToDelete().size}
              >
                Clear selection
              </button>
              <SlideToConfirm
                label={
                  deletingRuns()
                    ? runDeleteSummary().runs
                      ? `Slide to delete ${plural(runDeleteSummary().runs, "run directory", "run directories")}`
                      : "Select runs"
                    : deleteSummary().files
                      ? `Slide to delete ${plural(deleteSummary().files, "checkpoint")}`
                      : "Select checkpoints"
                }
                disabled={deletingRuns() ? !runDeleteSummary().runs : !deleteSummary().files}
                busy={deleting()}
                onConfirm={() => void confirmDelete()}
                testid="ckpt-slide"
              />
            </div>
          </Show>
          <Show when={!deleteMode() && !picking() && toSend().size > 0}>
            <div
              class="flex shrink-0 flex-wrap items-center gap-2 border-t border-line-weak bg-bg-stronger px-4 py-2.5"
              data-testid="ckpt-send-bar"
            >
              <span class="text-sm text-fg-strong">
                {plural(sendSummary().files, "checkpoint")} selected ·{" "}
                <span class="font-mono text-fg-weak">{formatBytes(sendSummary().size)}</span>
              </span>
              <button
                type="button"
                class="h-7 rounded px-2 text-xs text-fg-weak hover:text-fg-strong"
                onClick={() => setToSend(new Set<string>())}
              >
                Clear
              </button>
              <div class="flex-1" />
              <button
                type="button"
                class="flex h-7 items-center gap-1.5 rounded px-2 text-xs text-fg hover:bg-surface-hover hover:text-fg-strong"
                onClick={() => void copyPaths()}
                title="Copy the paths on the machine, one per line"
              >
                <IconCopy size={12} /> Copy paths
              </button>
              <Show
                when={targets().length}
                fallback={
                  <span class="text-xs text-fg-weak">
                    No project takes a <code class="font-mono">{"{checkpoint}"}</code> (add one with{" "}
                    <code class="font-mono">extends</code>)
                  </span>
                }
              >
                <span class="text-xs text-fg-weak">Send to</span>
                <select
                  class="h-7 rounded border border-line-weak bg-input px-1.5 text-sm text-fg-strong outline-none focus:border-line"
                  value={targetId() ?? ""}
                  onChange={(e) => setTarget(e.currentTarget.value)}
                  aria-label="Target project"
                  data-testid="ckpt-target"
                >
                  <For each={targets()}>{(p) => <option value={p.id}>{p.label}</option>}</For>
                </select>
                <button
                  type="button"
                  class="h-7 rounded-md bg-primary px-3 text-sm font-medium text-primary-fg"
                  onClick={sendTo}
                  data-testid="ckpt-send"
                >
                  Create {plural(toSend().size, "draft")}
                </button>
              </Show>
            </div>
          </Show>
        </Show>
      </Show>
    </div>
  )
}

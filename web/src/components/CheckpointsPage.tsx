import { Tooltip } from "@kobalte/core/tooltip"
import { useNavigate } from "@solidjs/router"
import { createEffect, createMemo, createSignal, For, type JSX, on, onCleanup, onMount, Show } from "solid-js"
import {
  type CheckState,
  checkState,
  hostPath,
  pruneSelection,
  type RunRowView,
  runFiles,
  type SortKey,
  selectionSummary,
  toggleFiles,
  viewRuns,
} from "~/lib/checkpoints"
import { formatAgo, formatBytes, formatDateTime } from "~/lib/format"
import type { CheckpointFile, CheckpointGroup, CheckpointSourceView, RunView } from "~/lib/types"
import { checkpointProjects, draftsFromCheckpoints, runsByCheckpoint, state } from "~/stores/app"
import { deleteCheckpoints, listings, loadCheckpoints } from "~/stores/checkpoints"
import { now } from "~/stores/clock"
import { toast } from "~/stores/toasts"
import {
  IconChevronRight,
  IconClose,
  IconCopy,
  IconRefresh,
  IconSearch,
  IconSpinner,
  IconTrash,
} from "./Icons"
import { SlideToConfirm } from "./SlideToConfirm"
import { statusLabel } from "./StatusDot"

const SOURCE_KEY = "trainhub.checkpoints.source"
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

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`

export function CheckpointsPage(props: { source?: string }) {
  const navigate = useNavigate()
  const sources = () => state.inventory?.checkpoint_sources ?? []
  const sourceId = createMemo(() => {
    const ids = sources().map((s) => s.id)
    const wanted = props.source ?? remembered()
    return wanted && ids.includes(wanted) ? wanted : (ids[0] ?? null)
  })
  const source = () => sources().find((s) => s.id === sourceId())
  const entry = () => (sourceId() ? listings[sourceId()!] : undefined)
  const listing = () => entry()?.data

  const [query, setQuery] = createSignal("")
  const [sort, setSort] = createSignal<SortKey>("date")
  const [hideEmpty, setHideEmpty] = createSignal(false)
  const [expanded, setExpanded] = createSignal<Set<string>>(new Set())
  const [collapsedGroups, setCollapsedGroups] = createSignal<Set<string>>(new Set())
  const [deleteMode, setDeleteMode] = createSignal(false)
  const [toDelete, setToDelete] = createSignal<Set<string>>(new Set())
  const [toSend, setToSend] = createSignal<Set<string>>(new Set())
  const [deleting, setDeleting] = createSignal(false)
  const [target, setTarget] = createSignal<string | null>(null)

  // Every source is loaded (the server caches the scans): their totals show in the header.
  createEffect(
    on(
      () => sources().map((s) => s.id),
      (ids) => {
        for (const id of ids) void loadCheckpoints(id)
      },
    ),
  )
  createEffect(
    on(sourceId, (id) => {
      if (!id) return
      remember(id)
      setExpanded(new Set<string>())
      setCollapsedGroups(new Set<string>())
      setToDelete(new Set<string>())
      setToSend(new Set<string>())
      setDeleteMode(false)
    }),
  )
  // Selections follow the listing (rescan, deletion from another client).
  createEffect(
    on(listing, (l) => {
      setToDelete((s) => pruneSelection(l, s))
      setToSend((s) => pruneSelection(l, s))
    }),
  )

  const rows = createMemo(() => viewRuns(listing(), { query: query(), sort: sort(), hideEmpty: hideEmpty() }))
  const byCheckpoint = createMemo(runsByCheckpoint)
  const targets = createMemo(checkpointProjects)
  const targetId = () => {
    const t = target()
    const list = targets()
    return t && list.some((p) => p.id === t) ? t : (list[0]?.id ?? null)
  }
  const deleteSummary = createMemo(() => selectionSummary(listing(), toDelete()))
  const sendSummary = createMemo(() => selectionSummary(listing(), toSend()))

  const isOpen = (r: RunRowView) => r.expand || expanded().has(r.run.name)
  const toggleRun = (name: string) =>
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  const groupKey = (run: string, g: string) => `${run}\u0000${g}`
  const toggleGroup = (run: string, g: string) =>
    setCollapsedGroups((s) => {
      const n = new Set(s)
      const k = groupKey(run, g)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })
  const allOpen = () => rows().length > 0 && rows().every(isOpen)

  const enterDelete = () => {
    setToSend(new Set<string>())
    setToDelete(new Set<string>())
    setDeleteMode(true)
  }
  const exitDelete = () => {
    setDeleteMode(false)
    setToDelete(new Set<string>())
  }

  onMount(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return
      if (deleteMode()) {
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

  const confirmDelete = async () => {
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

  const FileRow = (fp: { file: CheckpointFile; indent: string; deletable: boolean }) => {
    const path = () => (source() ? hostPath(source()!.root, fp.file.path) : fp.file.path)
    const runs = (): RunView[] => byCheckpoint()[path()] ?? []
    const selected = () => (deleteMode() ? toDelete() : toSend()).has(fp.file.path)
    const toggle = () => {
      if (deleteMode()) {
        if (fp.deletable) setToDelete((s) => toggleFiles([fp.file], s))
      } else setToSend((s) => toggleFiles([fp.file], s))
    }
    const writing = () => now() - Date.parse(fp.file.mtime) < WRITING_MS
    return (
      <div
        class={`flex h-8 cursor-pointer items-center gap-2 pr-4 hover:bg-surface-hover ${fp.indent}`}
        classList={{
          "bg-surface-active": selected() && !deleteMode(),
          "bg-critical-bg": selected() && deleteMode(),
          "opacity-50": deleteMode() && !fp.deletable,
        }}
        onClick={toggle}
        data-testid="ckpt-file"
        data-path={fp.file.path}
      >
        <Show
          when={!deleteMode() || fp.deletable}
          fallback={
            <span class="size-3.5 shrink-0" title="Not under a run directory: cannot be deleted here" />
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
    )
  }

  const GroupBlock = (gp: { run: string; group: CheckpointGroup; label: string; deletable: boolean }) => {
    const open = () => !collapsedGroups().has(groupKey(gp.run, gp.group.name))
    const sel = () => checkState(gp.group.files, toDelete())
    return (
      <div data-testid="ckpt-group" data-group={gp.group.name}>
        <div
          class="flex h-8 cursor-pointer items-center gap-2 pr-4 pl-10 hover:bg-surface-hover"
          onClick={() => toggleGroup(gp.run, gp.group.name)}
        >
          <IconChevronRight
            size={10}
            class={`shrink-0 text-icon transition-transform duration-150 ${open() ? "rotate-90" : ""}`}
          />
          <Show when={deleteMode() && gp.deletable && gp.group.files.length}>
            <Check
              state={sel()}
              onToggle={() => setToDelete((s) => toggleFiles(gp.group.files, s))}
              label={`Select the group ${gp.label}`}
              danger
              testid="ckpt-group-check"
            />
          </Show>
          <span class="min-w-0 flex-1 truncate font-mono text-sm text-fg" title={gp.group.path ?? undefined}>
            {gp.label}
          </span>
          <Cols count={plural(gp.group.count, "ckpt")} size={gp.group.size} />
        </div>
        <Show when={open()}>
          <For each={gp.group.files}>
            {(f) => <FileRow file={f} indent="pl-16" deletable={gp.deletable} />}
          </For>
        </Show>
      </div>
    )
  }

  const RunBlock = (rp: { row: RunRowView }) => {
    const run = () => rp.row.run
    const open = () => isOpen(rp.row)
    const filesSelected = () => {
      const sel = deleteMode() ? toDelete() : toSend()
      return runFiles(rp.row).filter((f) => sel.has(f.path)).length
    }
    return (
      <div class="border-b border-line-weaker" data-testid="ckpt-run" data-run={run().name}>
        <div
          class="flex h-10 cursor-pointer items-center gap-2 px-4 outline-none hover:bg-surface-hover focus-visible:bg-surface-hover"
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
            <For each={rp.row.groups}>
              {(g) => (
                <GroupBlock
                  run={run().name}
                  group={g}
                  label={g.name ? `${g.name}/` : "checkpoints"}
                  deletable={true}
                />
              )}
            </For>
            <Show when={rp.row.others.length}>
              <GroupBlock
                run={run().name}
                group={{
                  name: "\u0000others",
                  path: null,
                  size: rp.row.others.reduce((a, f) => a + f.size, 0),
                  count: rp.row.others.length,
                  files: rp.row.others,
                }}
                label="other files (outside the layout)"
                deletable={true}
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
        </div>
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
              <div class="border-b border-line-weaker px-4 py-1.5 text-xs text-fg-critical">
                Delete mode: tick checkpoints or whole groups, then slide to confirm at the bottom.
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
                when={deleteSummary().files}
                fallback={<span class="text-fg-weak">Nothing selected</span>}
              >
                <Show when={deleteSummary().groups}>{plural(deleteSummary().groups, "group")}, </Show>
                {plural(deleteSummary().files, "checkpoint")} in {plural(deleteSummary().runs, "run")} ·{" "}
                <span class="font-mono font-medium text-fg-critical">
                  {formatBytes(deleteSummary().size)}
                </span>
              </Show>
            </span>
            <div class="flex-1" />
            <button
              type="button"
              class="h-7 rounded px-2 text-xs text-fg-weak hover:text-fg-strong"
              onClick={() => setToDelete(new Set<string>())}
              disabled={!toDelete().size}
            >
              Clear selection
            </button>
            <SlideToConfirm
              label={
                deleteSummary().files
                  ? `Slide to delete ${plural(deleteSummary().files, "checkpoint")}`
                  : "Select checkpoints"
              }
              disabled={!deleteSummary().files}
              busy={deleting()}
              onConfirm={() => void confirmDelete()}
              testid="ckpt-slide"
            />
          </div>
        </Show>
        <Show when={!deleteMode() && toSend().size > 0}>
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
    </div>
  )
}

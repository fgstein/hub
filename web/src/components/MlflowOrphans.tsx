/** “Orphans”: MLflow runs whose logged directory is in no checkpoint source, and their deletion. */
import { createEffect, createMemo, createSignal, For, on, onMount, Show } from "solid-js"
import { formatAgo, formatDateTime } from "~/lib/format"
import type { MlflowOrphan } from "~/lib/types"
import { deleteOrphans, loadOrphans, orphans } from "~/stores/checkpoints"
import { now } from "~/stores/clock"
import { toast } from "~/stores/toasts"
import { IconMlflow, IconRefresh, IconSearch, IconSpinner } from "./Icons"
import { SlideToConfirm } from "./SlideToConfirm"

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`

const STATUS_COLOR: Record<string, string> = {
  RUNNING: "var(--status-green)",
  SCHEDULED: "var(--status-green)",
  FINISHED: "var(--status-blue)",
  FAILED: "var(--status-red)",
  KILLED: "var(--status-red)",
}

/** Directory logged by the run, split into its parent and its last component. */
function splitPath(path: string): [string, string] {
  const parts = path.replace(/\/+$/, "").split("/")
  // Lightning logs .../<run>/checkpoints: show the run directory, not "checkpoints".
  const last =
    parts.length > 1 && parts[parts.length - 1] === "checkpoints" ? parts.length - 2 : parts.length - 1
  return [`${parts.slice(0, last).join("/")}/`, parts.slice(last).join("/")]
}

export function MlflowOrphansView() {
  const [query, setQuery] = createSignal("")
  onMount(() => void loadOrphans("reload"))
  const data = () => orphans.data
  const rows = createMemo(() => {
    const q = query().trim().toLowerCase()
    const runs = data()?.runs ?? []
    if (!q) return runs
    return runs.filter(
      (o) =>
        o.path.toLowerCase().includes(q) ||
        o.run.run_name?.toLowerCase().includes(q) ||
        o.run.run_id.toLowerCase().startsWith(q),
    )
  })
  const [selected, setSelected] = createSignal<Set<string>>(new Set())
  const [deleting, setDeleting] = createSignal(false)
  // The selection follows the listing (deleted elsewhere, no longer an orphan).
  createEffect(
    on(data, (d) => {
      const ids = new Set((d?.runs ?? []).map((o) => o.run.run_id))
      setSelected((s) => new Set([...s].filter((id) => ids.has(id))))
    }),
  )
  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const allShown = () => rows().length > 0 && rows().every((o) => selected().has(o.run.run_id))
  const someShown = () => rows().some((o) => selected().has(o.run.run_id))
  const toggleShown = () =>
    setSelected((s) => {
      const n = new Set(s)
      const all = allShown()
      for (const o of rows()) {
        if (all) n.delete(o.run.run_id)
        else n.add(o.run.run_id)
      }
      return n
    })
  const runningSelected = () =>
    (data()?.runs ?? []).filter(
      (o) => selected().has(o.run.run_id) && (o.run.status === "RUNNING" || o.run.status === "SCHEDULED"),
    ).length
  const blocked = () => Object.keys(data()?.scan_errors ?? {}).length > 0

  const confirmDelete = async () => {
    const ids = [...selected()]
    if (!ids.length || deleting()) return
    setDeleting(true)
    const res = await deleteOrphans(ids)
    setDeleting(false)
    if (!res) return
    if (res.deleted.length) toast(`${plural(res.deleted.length, "MLflow run")} deleted`, "success")
    const failed = Object.entries(res.failed ?? {})
    if (failed.length) {
      const [id, err] = failed[0]!
      toast(
        `${plural(failed.length, "MLflow run")} not deleted: ${id}: ${err}${failed.length > 1 ? " (…)" : ""}`,
        "error",
        10000,
      )
      setSelected(new Set(failed.map(([i]) => i)))
    } else setSelected(new Set<string>())
  }

  const scanErrors = () => Object.entries(data()?.scan_errors ?? {})
  const mlflowErrors = () => Object.entries(data()?.mlflow_errors ?? {})

  const Row = (rp: { o: MlflowOrphan }) => {
    const run = () => rp.o.run
    const path = () => splitPath(rp.o.path)
    return (
      <div
        class="flex h-10 cursor-pointer items-center gap-2 border-b border-line-weaker px-4 hover:bg-surface-hover"
        classList={{ "bg-critical-bg": selected().has(run().run_id) }}
        onClick={() => toggle(run().run_id)}
        data-testid="mlflow-orphan"
      >
        <input
          type="checkbox"
          class="size-3.5 shrink-0 cursor-pointer accent-[var(--text-critical)]"
          checked={selected().has(run().run_id)}
          aria-label={`Select ${run().run_name ?? run().run_id}`}
          onClick={(e) => e.stopPropagation()}
          onChange={() => toggle(run().run_id)}
          data-testid="mlflow-orphan-check"
        />
        <span
          class="dot"
          style={{ "--dot-color": STATUS_COLOR[run().status ?? ""] ?? "var(--status-grey)" }}
          role="img"
          aria-label={run().status ?? "unknown status"}
          title={run().status ?? undefined}
        />
        <span class="w-48 shrink-0 truncate font-mono text-sm text-fg-strong" title={run().run_id}>
          {run().run_name ?? run().run_id}
        </span>
        <span class="min-w-0 flex-1 truncate font-mono text-xs" title={`${rp.o.match} = ${rp.o.path}`}>
          <span class="text-fg-weaker">{path()[0]}</span>
          <span class="text-fg">{path()[1]}</span>
        </span>
        <span class="shrink-0 text-xs text-fg-weaker" title="Sources whose MLflow search found the run">
          {rp.o.sources.join(", ")}
        </span>
        <span
          class="w-24 shrink-0 text-right text-xs tabular text-fg-weak"
          title={formatDateTime(run().started_at)}
        >
          {run().started_at ? formatAgo(run().started_at, now()) : ""}
        </span>
        <span class="flex w-7 shrink-0 justify-end">
          <Show when={run().url}>
            <a
              href={run().url!}
              target="_blank"
              rel="noopener noreferrer"
              class="flex size-6 items-center justify-center rounded hover:bg-surface-hover"
              aria-label="Open the run in MLflow"
              title="Open the run in MLflow"
              onClick={(e) => e.stopPropagation()}
              data-testid="mlflow-orphan-link"
            >
              <IconMlflow size={16} />
            </a>
          </Show>
        </span>
      </div>
    )
  }

  return (
    <>
      <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-line-weaker px-4 py-2">
        <input
          type="checkbox"
          class="size-3.5 shrink-0 cursor-pointer accent-[var(--text-critical)]"
          checked={allShown()}
          ref={(el) => createEffect(() => (el.indeterminate = someShown() && !allShown()))}
          disabled={!rows().length}
          aria-label="Select every run shown"
          onChange={toggleShown}
          data-testid="mlflow-orphans-check-all"
        />
        <span
          class="text-xs text-fg-weak"
          title="MLflow runs whose logged directory exists in no checkpoint source"
          data-testid="mlflow-orphans-summary"
        >
          <Show when={data()} fallback="Orphans: MLflow runs without a directory on disk">
            {(d) => (
              <>
                {plural(d().runs.length, "orphan MLflow run")} (no directory on disk) · {d().searched} read,{" "}
                {d().linked} with a directory
              </>
            )}
          </Show>
        </span>
        <Show when={data()?.searched_at}>
          <span class="text-xs text-fg-weaker" title={formatDateTime(data()?.searched_at)}>
            · searched {formatAgo(data()?.searched_at, now())}
          </span>
        </Show>
        <button
          type="button"
          class="flex size-6 shrink-0 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover disabled:opacity-40"
          title="Rescan the disks and MLflow"
          aria-label="Rescan the disks and MLflow"
          disabled={orphans.loading}
          onClick={() => void loadOrphans("rescan")}
          data-testid="mlflow-orphans-refresh"
        >
          <Show when={orphans.loading} fallback={<IconRefresh size={12} />}>
            <IconSpinner size={12} />
          </Show>
        </button>
        <div class="flex-1" />
        <label class="flex h-7 w-56 items-center gap-1.5 rounded border border-line-weak bg-input px-2 focus-within:border-line">
          <IconSearch size={12} class="shrink-0 text-icon" />
          <input
            value={query()}
            onInput={(e) => setQuery(e.currentTarget.value)}
            placeholder="Search runs, paths…"
            class="h-full min-w-0 flex-1 bg-transparent text-sm text-fg-strong outline-none placeholder:text-fg-weaker"
            aria-label="Search the MLflow runs"
          />
        </label>
      </div>
      <Show when={orphans.error}>
        <div class="shrink-0 border-b border-line-weaker bg-critical-bg px-4 py-1.5 text-xs text-fg-critical">
          {orphans.error}
        </div>
      </Show>
      <For each={scanErrors()}>
        {([src, err]) => (
          <div
            class="shrink-0 border-b border-line-weaker bg-critical-bg px-4 py-1.5 text-xs text-fg-critical"
            data-testid="mlflow-orphans-scan-error"
          >
            Source <span class="font-mono">{src}</span> could not be scanned ({err}): runs of its directories
            may be listed here by mistake.
          </div>
        )}
      </For>
      <For each={mlflowErrors()}>
        {([src, err]) => (
          <div class="shrink-0 border-b border-line-weaker px-4 py-1.5 text-xs text-fg-warning">
            MLflow search of <span class="font-mono">{src}</span>: {err}
          </div>
        )}
      </For>
      <Show when={data()?.truncated}>
        <div class="shrink-0 border-b border-line-weaker px-4 py-1.5 text-xs text-fg-warning">
          An MLflow search stopped early: only the most recent MLflow runs were read.
        </div>
      </Show>
      <div class="min-h-0 flex-1 overflow-y-auto" data-testid="mlflow-orphans">
        <Show
          when={data()}
          fallback={
            <div class="flex items-center gap-2 px-4 py-6 text-sm text-fg-weak">
              <Show when={orphans.loading}>
                <IconSpinner size={12} /> Scanning the sources and MLflow…
              </Show>
            </div>
          }
        >
          <For
            each={rows()}
            fallback={
              <div class="px-4 py-6 text-sm text-fg-weak">
                {query()
                  ? "Nothing matches the search."
                  : "No orphan: every MLflow run has its directory in a checkpoint source."}
              </div>
            }
          >
            {(o) => <Row o={o} />}
          </For>
        </Show>
      </div>
      <Show when={selected().size}>
        <div
          class="flex shrink-0 flex-wrap items-center gap-3 border-t border-line-critical bg-bg-stronger px-4 py-2.5"
          data-testid="mlflow-orphans-delete-bar"
        >
          <span class="text-sm text-fg-strong">
            <span class="font-medium text-fg-critical">{plural(selected().size, "MLflow run")}</span> to
            delete
            <Show when={runningSelected()}>
              <span class="text-fg-warning"> · {runningSelected()} still running</span>
            </Show>
          </span>
          <span class="text-xs text-fg-weak">
            MLflow trash: restorable until <code class="font-mono">mlflow gc</code>
          </span>
          <div class="flex-1" />
          <button
            type="button"
            class="h-7 rounded px-2 text-xs text-fg-weak hover:text-fg-strong"
            onClick={() => setSelected(new Set<string>())}
          >
            Clear selection
          </button>
          <SlideToConfirm
            label={
              blocked()
                ? "A source could not be scanned"
                : `Slide to delete ${plural(selected().size, "run")}`
            }
            disabled={blocked()}
            busy={deleting()}
            onConfirm={() => void confirmDelete()}
            testid="mlflow-orphans-slide"
          />
        </div>
      </Show>
    </>
  )
}

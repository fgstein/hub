import { Popover } from "@kobalte/core/popover"
import { A, useNavigate } from "@solidjs/router"
import { createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js"
import { formatGiB } from "~/lib/gpus"
import { loadColor, percent, summarize } from "~/lib/load"
import type { CpuView, GpuView, MachineView } from "~/lib/types"
import { draftOnGpu, loadCache, loadMachine, state } from "~/stores/app"
import { IconRefresh, IconSpinner } from "./Icons"

/** Refresh period of the tab (each machine is queried in parallel, one request at a time). */
const REFRESH_MS = 5000

/** Horizontal meter: one hue, value written next to it (never color alone). */
function Meter(props: {
  pct: number | null
  label: string
  title?: string
  testid?: string
  labelClass?: string
}) {
  return (
    <span class="flex min-w-0 items-center gap-2" title={props.title} data-testid={props.testid}>
      <span class="relative h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-surface-active">
        <span
          class="absolute inset-y-0 left-0 rounded-full transition-[width] duration-500"
          style={{ width: `${props.pct ?? 0}%`, background: "var(--load-full)" }}
        />
      </span>
      <span class={`shrink-0 text-right font-mono text-xs text-fg tabular ${props.labelClass ?? ""}`}>
        {props.label}
      </span>
    </span>
  )
}

const pctLabel = (p: number | null | undefined) => (p == null ? "?" : `${Math.round(p)}%`)

function CpuSection(props: { cpu: CpuView }) {
  const mem = () => percent(props.cpu.memory_used_mib, props.cpu.memory_total_mib)
  return (
    <div class="flex flex-col gap-2" data-testid="machine-cpu">
      <div class="grid grid-cols-[64px_1fr] items-center gap-x-3 gap-y-1.5 text-xs">
        <span class="text-fg-weak">CPU</span>
        <Meter
          pct={props.cpu.usage_pct}
          label={`${pctLabel(props.cpu.usage_pct)} · ${props.cpu.cores} cores`}
          testid="machine-cpu-usage"
          labelClass="w-[120px]"
        />
        <span class="text-fg-weak">Memory</span>
        <Meter
          pct={mem()}
          label={`${formatGiB(props.cpu.memory_used_mib)}/${formatGiB(props.cpu.memory_total_mib)} GiB`}
          labelClass="w-[120px]"
        />
        <Show when={props.cpu.load_avg}>
          <span class="text-fg-weak">Load</span>
          <span class="font-mono text-fg tabular" title="Load average over 1, 5 and 15 minutes">
            {props.cpu.load_avg!.map((v) => v.toFixed(1)).join(" · ")}
          </span>
        </Show>
      </div>
      {/* One cell per core: the landscape of the machine at a glance. */}
      <div
        class="flex flex-wrap gap-0.5"
        role="list"
        aria-label="Usage of each core"
        data-testid="machine-cores"
      >
        <For each={props.cpu.per_core_pct}>
          {(p, i) => (
            <span
              role="listitem"
              class="size-2.5 rounded-[2px]"
              style={{ background: loadColor(p) }}
              title={`core ${i()}: ${pctLabel(p)}`}
              aria-label={`core ${i()}: ${pctLabel(p)}`}
            />
          )}
        </For>
      </div>
    </div>
  )
}

/** Projects allowed to run on a machine (a GPU click prepares a draft in one of them). */
const projectsOn = (machine: string) =>
  (state.inventory?.projects ?? []).filter((p) => p.machines.includes(machine))

function GpuRow(props: { gpu: GpuView; machine: string }) {
  const navigate = useNavigate()
  const [open, setOpen] = createSignal(false)
  const mem = () => percent(props.gpu.memory_used_mib, props.gpu.memory_total_mib)
  const projectOf = (rid: string) => state.runs[rid]?.project_id
  const projects = () => projectsOn(props.machine)
  const prepare = (projectId: string) => {
    setOpen(false)
    draftOnGpu(projectId, props.machine, props.gpu.index)
    navigate(`/p/${encodeURIComponent(projectId)}`)
  }
  const title = () =>
    projects().length
      ? `New run on ${props.machine}, GPU ${props.gpu.index} (CUDA_VISIBLE_DEVICES=${props.gpu.index})`
      : `No project can run on ${props.machine}`
  return (
    <div class="flex flex-col" data-testid="machine-gpu" data-index={props.gpu.index}>
      <Popover
        open={open()}
        onOpenChange={(o) => {
          if (!o) return setOpen(false)
          const list = projects()
          // A single possible project: no menu, the draft is ready at once.
          if (list.length === 1) prepare(list[0]!.id)
          else if (list.length > 1) setOpen(true)
        }}
        placement="bottom-start"
        gutter={2}
      >
        <Popover.Trigger
          as="button"
          type="button"
          disabled={projects().length === 0}
          title={title()}
          class="-mx-1.5 grid cursor-pointer grid-cols-[22px_minmax(0,1fr)_minmax(0,1fr)_40px] items-center gap-x-3 rounded px-1.5 py-1 text-left text-xs hover:bg-surface-hover disabled:cursor-default disabled:hover:bg-transparent sm:grid-cols-[22px_minmax(0,0.9fr)_minmax(0,1fr)_minmax(0,1fr)_40px]"
          classList={{ "bg-surface-active": open() }}
          data-testid="machine-gpu-launch"
        >
          <span class="flex items-center gap-1 font-mono text-fg-strong">
            <span
              class="size-2.5 shrink-0 rounded-[2px]"
              style={{ background: loadColor(props.gpu.utilization_pct) }}
              aria-hidden="true"
            />
            {props.gpu.index}
          </span>
          <span class="hidden min-w-0 truncate text-fg-weak sm:block" title={props.gpu.name}>
            {props.gpu.name}
          </span>
          <Meter
            pct={props.gpu.utilization_pct ?? null}
            label={pctLabel(props.gpu.utilization_pct)}
            title="Utilization"
          />
          <Meter
            pct={mem()}
            label={`${formatGiB(props.gpu.memory_used_mib)}/${formatGiB(props.gpu.memory_total_mib)}G`}
            title="Memory"
          />
          <span class="text-right font-mono text-fg-weak tabular" title="Temperature">
            {props.gpu.temperature_c ?? "?"}°
          </span>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            class="menu-content z-[70] w-60 rounded-md bg-float p-1 shadow-float outline-none"
            data-testid="gpu-project-menu"
          >
            <div class="px-2 pt-1 pb-1.5 text-xxs text-fg-weak">
              New run on <span class="font-mono text-fg-strong">{props.machine}</span> · GPU{" "}
              <span class="font-mono text-fg-strong">{props.gpu.index}</span>
            </div>
            <For each={projects()}>
              {(p) => (
                <button
                  type="button"
                  class="flex h-7 w-full items-center gap-2 rounded px-2 text-left text-sm text-fg-strong hover:bg-surface-active"
                  onClick={() => prepare(p.id)}
                  data-testid={`gpu-project-${p.id}`}
                >
                  <span class="truncate">{p.label}</span>
                  <span class="ml-auto font-mono text-xs text-fg-weak">{p.id}</span>
                </button>
              )}
            </For>
          </Popover.Content>
        </Popover.Portal>
      </Popover>
      <Show when={props.gpu.runs.length || props.gpu.processes}>
        <div class="flex flex-wrap items-center gap-1 pb-1 pl-[34px] text-xs">
          <For each={props.gpu.runs}>
            {(rid) => (
              <Show
                when={projectOf(rid)}
                fallback={
                  <span class="rounded bg-surface-active px-1 font-mono text-xxs text-fg-strong">{rid}</span>
                }
              >
                <A
                  href={`/p/${encodeURIComponent(projectOf(rid)!)}`}
                  class="rounded bg-surface-active px-1 font-mono text-xxs text-fg-strong hover:bg-surface-hover"
                  title={`Run ${rid} (${projectOf(rid)})`}
                >
                  {rid}
                </A>
              </Show>
            )}
          </For>
          <Show when={props.gpu.runs.length === 0}>
            <span class="text-xxs text-fg-weak">
              {props.gpu.processes} process{props.gpu.processes === 1 ? "" : "es"} (outside TrainHub)
            </span>
          </Show>
        </div>
      </Show>
    </div>
  )
}

function MachineCard(props: { machine: MachineView }) {
  const entry = () => loadCache[props.machine.id]
  const data = () => entry()?.data
  const reachable = () => (data() ? data()!.reachable : props.machine.status.reachable)
  const gpus = () => data()?.gpus ?? []
  const activeRuns = () => data()?.active_runs ?? []
  const unrestricted = () => activeRuns().filter((r) => r.cuda_visible_devices == null)
  return (
    <section
      class="flex min-w-0 flex-col gap-3 rounded-lg border border-line-weak bg-bg-strong p-4"
      data-testid={`machine-card-${props.machine.id}`}
      data-reachable={reachable() == null ? undefined : String(reachable())}
    >
      <header class="flex min-w-0 items-center gap-2">
        <span
          class="dot"
          style={{
            "--dot-color":
              reachable() == null ? undefined : reachable() ? "var(--status-green)" : "var(--status-red)",
          }}
          role="img"
          aria-label={reachable() == null ? "not checked yet" : reachable() ? "reachable" : "unreachable"}
        />
        <h2 class="truncate text-sm font-medium text-fg-strong">{props.machine.label ?? props.machine.id}</h2>
        <span class="truncate font-mono text-xs text-fg-weak">
          {props.machine.id} · {props.machine.user ? `${props.machine.user}@` : ""}
          {props.machine.host}
        </span>
        <span class="ml-auto shrink-0 text-xs text-fg-weaker">
          <Show when={entry()?.loading && !data()} fallback={null}>
            <IconSpinner size={12} />
          </Show>
          <Show when={activeRuns().length > 0}>
            <span class="text-fg-weak">
              {activeRuns().length} active run{activeRuns().length === 1 ? "" : "s"}
            </span>
          </Show>
        </span>
      </header>
      <Show when={entry()?.error || (data() && !data()!.reachable)}>
        <p class="text-xs text-fg-critical" data-testid="machine-error">
          {entry()?.error ?? data()?.error ?? "unreachable"}
        </p>
      </Show>
      <Show when={data()?.reachable}>
        <Show
          when={data()!.cpu}
          fallback={<p class="text-xs text-fg-weak">{data()!.cpu_error ?? "CPU load unavailable"}</p>}
        >
          <CpuSection cpu={data()!.cpu!} />
        </Show>
        <div class="flex flex-col border-t border-line-weaker pt-2">
          <Show
            when={gpus().length}
            fallback={<p class="text-xs text-fg-weak">{data()!.gpu_error ?? "No GPU"}</p>}
          >
            <div class="mb-0.5 flex text-xs text-fg-weak">
              GPU · {gpus().length} · {gpus().filter((g) => g.processes > 0).length} busy
              <span class="ml-auto text-xxs text-fg-weaker">click a GPU to prepare a run on it</span>
            </div>
            <For each={gpus()}>{(g) => <GpuRow gpu={g} machine={props.machine.id} />}</For>
          </Show>
          <Show when={unrestricted().length}>
            <p class="pt-1 text-xxs text-fg-weak">
              +{unrestricted().length} run{unrestricted().length === 1 ? "" : "s"} without
              CUDA_VISIBLE_DEVICES (every GPU visible):{" "}
              {unrestricted()
                .map((r) => r.id)
                .join(", ")}
            </p>
          </Show>
        </div>
      </Show>
    </section>
  )
}

/** “Machines” tab: CPU and GPU load of every machine of the inventory. */
export function MachinesPage() {
  const machines = () => state.inventory?.machines ?? []
  const refreshAll = () => {
    for (const m of machines()) void loadMachine(m.id)
  }
  onMount(() => {
    refreshAll()
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refreshAll()
    }, REFRESH_MS)
    onCleanup(() => clearInterval(timer))
  })
  const summary = createMemo(() => summarize(machines().map((m) => loadCache[m.id]?.data)))
  const loading = () => machines().some((m) => loadCache[m.id]?.loading)

  return (
    <div class="min-h-0 flex-1 overflow-y-auto" data-testid="machines-page">
      <div class="mx-auto flex max-w-[1400px] flex-col gap-4 p-4">
        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <h1 class="font-medium text-fg-strong">Machines</h1>
          <span class="text-fg-weak" data-testid="machines-summary">
            {summary().reachable}/{summary().machines} reachable · {summary().busyGpus}/{summary().gpus} GPUs
            busy
            <Show when={summary().cpuPct != null}>
              {" "}
              · CPU {Math.round(summary().cpuPct!)}% of {summary().cores} cores
            </Show>
          </span>
          <span class="ml-auto flex items-center gap-2 text-xxs text-fg-weak">
            <span>0%</span>
            <span
              class="h-2 w-24 rounded-[2px]"
              style={{ background: "linear-gradient(to right, var(--load-empty), var(--load-full))" }}
              aria-hidden="true"
            />
            <span>100%</span>
          </span>
          <button
            type="button"
            class="flex size-7 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
            title={`Refresh (automatic every ${REFRESH_MS / 1000} s)`}
            aria-label="Refresh the machines"
            onClick={refreshAll}
            data-testid="machines-refresh"
          >
            <Show when={loading()} fallback={<IconRefresh size={12} />}>
              <IconSpinner size={12} />
            </Show>
          </button>
        </div>
        <Show
          when={machines().length}
          fallback={<p class="text-sm text-fg-weak">No machine in the inventory.</p>}
        >
          <div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <For each={machines()}>{(m) => <MachineCard machine={m} />}</For>
          </div>
        </Show>
      </div>
    </div>
  )
}

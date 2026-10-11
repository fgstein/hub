import { Popover } from "@kobalte/core/popover"
import { createMemo, createSignal, For, Show } from "solid-js"
import { formatGiB, parseIndices, toggleIndex } from "~/lib/gpus"
import type { GpuView } from "~/lib/types"
import { gpusCache, loadGpus } from "~/stores/app"
import { IconChevronDown, IconRefresh, IconSpinner } from "./Icons"

/** A GPU is busy as soon as a compute process runs on it (any user). */
const busy = (g: GpuView) => g.processes > 0

function MemoryBar(props: { gpu: GpuView }) {
  const pct = () => {
    const { memory_used_mib: used, memory_total_mib: total } = props.gpu
    return used != null && total ? Math.min(100, (100 * used) / total) : 0
  }
  return (
    <span class="relative h-1.5 w-14 shrink-0 overflow-hidden rounded-full bg-surface-active">
      <span
        class="absolute inset-y-0 left-0 rounded-full"
        style={{
          width: `${pct()}%`,
          background:
            pct() > 80 ? "var(--status-red)" : pct() > 10 ? "var(--status-orange)" : "var(--status-green)",
        }}
      />
    </span>
  )
}

/**
 * CUDA_VISIBLE_DEVICES field: free typing, plus a list of the machine's GPUs (nvidia-smi)
 * with their memory, utilization and the TrainHub runs on them; a click toggles a GPU.
 */
export function GpuPicker(props: { machine: string | null; value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = createSignal(false)
  let anchor: HTMLDivElement | undefined
  const entry = () => (props.machine ? gpusCache[props.machine] : undefined)
  const gpus = () => entry()?.data?.gpus ?? []
  const selected = createMemo(() => new Set(parseIndices(props.value) ?? []))
  const unrestricted = () => (entry()?.data?.active_runs ?? []).filter((r) => r.cuda_visible_devices == null)

  const refresh = (force = false) => {
    if (props.machine) void loadGpus(props.machine, force ? 0 : undefined)
  }

  return (
    <Popover
      open={open()}
      onOpenChange={(o) => {
        setOpen(o)
        if (o) refresh()
      }}
      anchorRef={() => anchor}
      placement="bottom-end"
      gutter={4}
    >
      <div
        ref={anchor}
        class="flex h-7 min-w-0 items-center rounded border border-line-weak bg-input focus-within:border-line"
      >
        <input
          value={props.value}
          onInput={(e) => props.onChange(e.currentTarget.value)}
          placeholder="GPU"
          title="CUDA_VISIBLE_DEVICES (optional), e.g. 0,1"
          aria-label="CUDA_VISIBLE_DEVICES"
          data-gpu=""
          data-testid="field-gpu"
          class="h-full w-full min-w-0 bg-transparent pl-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker"
        />
        <Popover.Trigger
          tabIndex={-1}
          class="flex h-full shrink-0 items-center px-1 text-icon hover:text-icon-hover"
          title="GPUs of the machine (nvidia-smi)"
          aria-label="Show GPUs"
          data-testid="gpu-trigger"
        >
          <IconChevronDown size={11} />
        </Popover.Trigger>
      </div>
      <Popover.Portal>
        <Popover.Content
          class="menu-content z-[70] flex w-[380px] max-w-[calc(100vw-2rem)] flex-col rounded-md bg-float shadow-float outline-none"
          // Events from a Solid portal bubble up to the draft: Enter/Escape must neither
          // launch the run nor close the draft.
          onKeyDown={(e) => e.stopPropagation()}
          data-testid="gpu-picker"
        >
          <div class="flex items-center gap-2 border-b border-line-weaker py-1 pr-1 pl-3">
            <span class="text-xs text-fg-weak">
              GPUs
              <Show when={props.machine}>
                {" "}
                · <span class="font-mono text-fg-strong">{props.machine}</span>
              </Show>
            </span>
            <Show when={props.machine}>
              <button
                type="button"
                class="ml-auto flex size-7 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
                title="Refresh (nvidia-smi on the machine)"
                aria-label="Refresh GPUs"
                onClick={() => refresh(true)}
                data-testid="gpu-refresh"
              >
                <Show when={entry()?.loading} fallback={<IconRefresh size={12} />}>
                  <IconSpinner size={12} />
                </Show>
              </button>
            </Show>
          </div>
          <div class="max-h-[320px] overflow-y-auto p-1" role="listbox" aria-multiselectable="true">
            <For each={gpus()}>
              {(g) => (
                <button
                  type="button"
                  role="option"
                  aria-selected={selected().has(g.index)}
                  class="flex h-8 w-full cursor-default items-center gap-2 rounded px-2 text-left text-sm hover:bg-surface-active"
                  title={`${g.name} · ${g.temperature_c ?? "?"} °C · ${g.processes} process${g.processes === 1 ? "" : "es"}`}
                  onClick={() => props.onChange(toggleIndex(props.value, g.index))}
                  data-testid="gpu-row"
                  data-index={g.index}
                  data-busy={busy(g) ? "" : undefined}
                >
                  <span class="w-3 shrink-0 text-xs text-fg-interactive">
                    {selected().has(g.index) ? "✓" : ""}
                  </span>
                  <span
                    class="dot shrink-0"
                    style={{
                      width: "6px",
                      height: "6px",
                      "--dot-color": busy(g) ? "var(--status-orange)" : "var(--status-green)",
                    }}
                  />
                  <span class="w-4 shrink-0 font-mono text-fg-strong">{g.index}</span>
                  <MemoryBar gpu={g} />
                  <span class="w-[78px] shrink-0 font-mono text-xs text-fg tabular">
                    {formatGiB(g.memory_used_mib)}/{formatGiB(g.memory_total_mib)}G
                  </span>
                  <span class="w-9 shrink-0 text-right font-mono text-xs text-fg-weak tabular">
                    {g.utilization_pct ?? "?"}%
                  </span>
                  <span class="ml-auto flex min-w-0 gap-1 overflow-hidden">
                    <For each={g.runs}>
                      {(rid) => (
                        <span class="shrink-0 rounded bg-surface-active px-1 font-mono text-xxs text-fg-strong">
                          {rid}
                        </span>
                      )}
                    </For>
                    <Show when={busy(g) && g.runs.length === 0}>
                      <span class="truncate text-xxs text-fg-weak">
                        {g.processes} proc{g.processes === 1 ? "" : "s"}
                      </span>
                    </Show>
                  </span>
                </button>
              )}
            </For>
            <Show when={gpus().length === 0}>
              <div class="px-2 py-2 text-sm text-fg-weak" data-testid="gpu-empty">
                {!props.machine
                  ? "Choose a machine first"
                  : entry()?.loading
                    ? "Querying nvidia-smi…"
                    : (entry()?.error ?? "No GPU found")}
              </div>
            </Show>
          </div>
          <div class="flex items-center gap-3 border-t border-line-weaker px-3 py-1.5 text-xxs text-fg-weak">
            <button
              type="button"
              class="rounded px-1 hover:bg-surface-hover hover:text-fg-strong"
              classList={{ "text-fg-strong": selected().size === 0 }}
              onClick={() => props.onChange("")}
              title="No CUDA_VISIBLE_DEVICES: the run sees every GPU"
              data-testid="gpu-all"
            >
              all GPUs
            </button>
            <Show when={unrestricted().length}>
              <span
                class="truncate"
                title={`Active runs without CUDA_VISIBLE_DEVICES (they see every GPU): ${unrestricted()
                  .map((r) => r.id)
                  .join(", ")}`}
              >
                +{unrestricted().length} run{unrestricted().length === 1 ? "" : "s"} on all GPUs
              </span>
            </Show>
            <span class="ml-auto flex items-center gap-1">
              <span
                class="dot"
                style={{ "--dot-color": "var(--status-green)", width: "6px", height: "6px" }}
              />
              free
            </span>
            <span class="flex items-center gap-1">
              <span
                class="dot"
                style={{ "--dot-color": "var(--status-orange)", width: "6px", height: "6px" }}
              />
              busy
            </span>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover>
  )
}

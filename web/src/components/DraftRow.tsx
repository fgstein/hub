import { Combobox } from "@kobalte/core/combobox"
import { createEffect, createMemo, For, on, onMount, Show } from "solid-js"
import { splitArgs } from "~/lib/command"
import type { ProjectView } from "~/lib/types"
import {
  commandErrors,
  compatibleVenvs,
  configKey,
  configsCache,
  type Draft,
  draftParamErrors,
  draftSelection,
  launchDraft,
  loadConfigs,
  machineReachable,
  removeDraft,
  state,
  updateDraft,
} from "~/stores/app"
import { ConfigPicker } from "./ConfigPicker"
import { GpuPicker } from "./GpuPicker"
import { IconChevronDown, IconClose, IconRefresh, IconSpinner } from "./Icons"
import { StatusDot } from "./StatusDot"

interface FieldProps {
  name: string
  label: string
  value: string | null
  options: string[]
  onChange: (v: string) => void
  loading?: boolean
  onRefresh?: () => void
  describe?: (v: string) => string | undefined
  error?: string | null
  testid: string
  autofocus?: boolean
}

/** A searchable list; a fixed label if only one option is possible. */
function Field(props: FieldProps) {
  let controlEl: HTMLDivElement | undefined
  const single = () => props.options.length === 1 && !props.loading && !props.onRefresh
  return (
    <Show
      when={!single()}
      fallback={
        <span
          class="flex h-7 min-w-0 items-center truncate rounded px-2 font-mono text-sm text-fg-strong"
          title={props.describe?.(props.options[0] ?? "") ?? props.label}
          data-testid={props.testid}
          data-fixed=""
        >
          {props.options[0]}
        </span>
      }
    >
      <Combobox<string>
        options={props.options}
        value={props.value}
        onChange={(v) => v && props.onChange(v)}
        placeholder={props.label}
        triggerMode="focus"
        defaultFilter={(opt, input) =>
          !input || input === props.value || opt.toLowerCase().includes(input.toLowerCase())
        }
        gutter={4}
        sameWidth={false}
        itemComponent={(p) => (
          <Combobox.Item
            item={p.item}
            class="flex h-7 cursor-default items-center gap-2 rounded px-2 font-mono text-sm text-fg-strong outline-none data-[highlighted]:bg-surface-active"
          >
            <Combobox.ItemLabel class="truncate">{p.item.rawValue}</Combobox.ItemLabel>
            <Show when={props.describe?.(p.item.rawValue)}>
              <span class="ml-auto truncate pl-3 font-sans text-xs text-fg-weak">
                {props.describe?.(p.item.rawValue)}
              </span>
            </Show>
          </Combobox.Item>
        )}
      >
        <Combobox.Control
          ref={controlEl}
          aria-label={props.label}
          class="flex h-7 min-w-0 items-center rounded border bg-input transition-colors hover:bg-input-hover focus-within:border-line"
          classList={{ "border-line-weak": !props.error, "border-line-critical": !!props.error }}
          data-testid={props.testid}
        >
          <Combobox.Input
            class="h-full w-full min-w-0 bg-transparent pl-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker"
            autofocus={props.autofocus}
            name={props.name}
          />
          <Show when={props.loading}>
            <IconSpinner size={11} class="mr-1 shrink-0 text-icon" />
          </Show>
          <Show when={props.onRefresh && !props.loading}>
            <button
              type="button"
              tabIndex={-1}
              class="shrink-0 px-0.5 text-icon hover:text-icon-hover"
              title="Refresh the list (discovery on the machine)"
              aria-label="Refresh configs"
              onClick={(e) => {
                e.stopPropagation()
                props.onRefresh?.()
              }}
            >
              <IconRefresh size={11} />
            </button>
          </Show>
          <Combobox.Trigger
            tabIndex={-1}
            class="flex h-full shrink-0 items-center px-1 text-icon hover:text-icon-hover"
          >
            <Combobox.Icon>
              <IconChevronDown size={11} />
            </Combobox.Icon>
          </Combobox.Trigger>
        </Combobox.Control>
        <Combobox.Portal>
          <Combobox.Content
            class="menu-content z-[70] min-w-[180px] max-w-[420px] rounded-md bg-float p-0.5 shadow-float outline-none"
            onCloseAutoFocus={(e) => {
              // Closed by a click on another field: do not take focus back from it (otherwise
              // the next keystrokes land in this field).
              const active = document.activeElement
              if (active && active !== document.body && !controlEl?.contains(active)) e.preventDefault()
            }}
          >
            <Combobox.Listbox class="max-h-72 overflow-y-auto outline-none" />
            <Show when={props.options.length === 0}>
              <div class="px-2 py-1.5 text-xs text-fg-weak">{props.loading ? "loading…" : "no options"}</div>
            </Show>
          </Combobox.Content>
        </Combobox.Portal>
      </Combobox>
    </Show>
  )
}

export function DraftRow(props: { project: ProjectView; draft: Draft; autofocus?: boolean }) {
  // “List open” state recorded BEFORE Kobalte handles the key (capture phase):
  // Enter in an open list selects an option, it does not launch the run.
  let listOpenAtKeydown = false
  const p = () => props.project
  const d = () => props.draft
  const set = (patch: Partial<Draft>) => updateDraft(p().id, d().key, patch)
  const venvs = createMemo(() => compatibleVenvs(p(), d().image))
  const cfgKey = () => configKey(p().id, p().discover ? d().machine : null)
  const cfg = () => configsCache[cfgKey()]
  const configs = createMemo(() => cfg()?.data?.configs ?? p().static_configs ?? [])

  // Discovered configs load once the machine is chosen.
  createEffect(
    on(cfgKey, () => {
      void loadConfigs(p().id, p().discover ? d().machine : null)
    }),
  )
  // Automatic selection when a single config is available.
  createEffect(() => {
    const list = configs()
    if (list.length === 1 && d().config !== list[0]) set({ config: list[0] ?? null })
  })

  const overridesError = () => splitArgs(d().overrides).error
  // The overrides field only shows up for a draft that inherits some (relaunch of a run
  // launched with overrides through the API, MCP or the CLI): otherwise, the command's {name}
  // parameters fill that role.
  const showOverrides = d().overrides.trim() !== ""
  const params = () => p().params ?? []
  const takesCheckpoint = () => !!p().accepts_checkpoint
  /** Source and run of the checkpoint, when it comes from a checkpoint source. */
  const checkpointOrigin = () => {
    const path = d().checkpoint.trim()
    for (const src of state.inventory?.checkpoint_sources ?? []) {
      const root = `${src.root.replace(/\/+$/, "")}/`
      if (path.startsWith(root)) return `${src.label} · ${path.slice(root.length).split("/")[0]}`
    }
    return null
  }
  const paramErrors = () => draftParamErrors(p().id, d())
  const sel = () => draftSelection(p().id, d())
  const blocked = () => commandErrors[p().id] === true
  const canLaunch = () => !!sel() && !blocked() && !d().launching

  // Draft coming from “Relaunch”: scrolled into view and briefly highlighted.
  let rowEl: HTMLDivElement | undefined
  onMount(() => {
    if (!d().focus) return
    rowEl?.scrollIntoView({ block: "center", behavior: "smooth" })
    setTimeout(() => set({ focus: false }), 900)
  })

  const describeMachine = (id: string) => {
    const m = state.inventory?.machines.find((x) => x.id === id)
    const r = machineReachable(id)
    return [m?.label, r === false ? "unreachable" : undefined].filter(Boolean).join(" · ") || undefined
  }
  const describeImage = (id: string) => state.inventory?.images.find((x) => x.id === id)?.image
  const describeVenv = (id: string) =>
    state.inventory?.venvs.find((x) => x.id === id)?.path ??
    (id === "system" ? "no venv (image Python)" : undefined)

  return (
    <div
      class="anim-in border-b border-line-weaker px-4 transition-colors duration-700"
      classList={{ "opacity-70": d().launching, "bg-surface-active": !!d().focus }}
      data-testid="draft-row"
      ref={(el) => {
        rowEl = el
        el.addEventListener(
          "keydown",
          (e) => {
            listOpenAtKeydown = (e.target as HTMLElement).getAttribute("aria-expanded") === "true"
          },
          true,
        )
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          // Escape first closes an open list (handled by Kobalte), then the draft.
          if (listOpenAtKeydown) return
          e.preventDefault()
          removeDraft(p().id, d().key)
        } else if (e.key === "Enter" && !listOpenAtKeydown) {
          e.preventDefault()
          if (canLaunch()) void launchDraft(p().id, d().key)
        }
      }}
    >
      <div class="flex h-12 items-center gap-3">
        <StatusDot status={d().launching ? "launching" : "draft"} />
        <span class="w-[68px] shrink-0 text-xs text-fg-weak">{d().launching ? "launching" : "draft"}</span>
        <div class="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.7fr)_minmax(0,1.7fr)_64px] items-center gap-1.5">
          <Field
            name="machine"
            label="machine"
            value={d().machine}
            options={p().machines}
            onChange={(v) => set({ machine: v })}
            describe={describeMachine}
            testid="field-machine"
            autofocus={props.autofocus}
          />
          <Field
            name="image"
            label="image"
            value={d().image}
            options={p().images}
            onChange={(v) => set({ image: v })}
            describe={describeImage}
            testid="field-image"
          />
          <Field
            name="venv"
            label="venv"
            value={d().venv}
            options={venvs()}
            onChange={(v) => set({ venv: v })}
            describe={describeVenv}
            testid="field-venv"
          />
          <ConfigPicker
            projectId={p().id}
            value={d().config}
            configs={configs()}
            root={cfg()?.data?.root}
            loading={cfg()?.loading}
            error={cfg()?.error}
            onChange={(v) => set({ config: v })}
            onRefresh={p().discover ? () => void loadConfigs(p().id, d().machine, true) : undefined}
          />
          <GpuPicker machine={d().machine} value={d().cuda} onChange={(v) => set({ cuda: v })} />
        </div>
        <div class="flex shrink-0 items-center gap-1">
          <button
            type="button"
            class="h-7 rounded-md bg-primary px-3 text-sm font-medium text-primary-fg transition-opacity disabled:opacity-30"
            disabled={!canLaunch()}
            title={
              blocked()
                ? "Invalid command: {config} missing"
                : takesCheckpoint() && !d().checkpoint.trim()
                  ? "Choose a checkpoint (Checkpoints tab, or paste its path)"
                  : overridesError()
                    ? `Invalid overrides: ${overridesError()}`
                    : Object.keys(paramErrors()).length
                      ? `Invalid parameter: {${Object.keys(paramErrors())[0]}}`
                      : !sel()
                        ? "Incomplete selection"
                        : "Launch (Enter)"
            }
            onClick={() => void launchDraft(p().id, d().key)}
            data-testid="draft-launch"
          >
            Launch
          </button>
          <button
            type="button"
            class="flex size-7 items-center justify-center rounded-md text-icon hover:bg-surface-hover hover:text-icon-hover"
            aria-label="Cancel draft (Escape)"
            title="Cancel (Escape)"
            onClick={() => removeDraft(p().id, d().key)}
            data-testid="draft-cancel"
          >
            <IconClose size={12} />
          </button>
        </div>
      </div>
      <Show when={params().length || showOverrides || takesCheckpoint()}>
        <div
          class="-mt-1 flex flex-wrap items-center gap-x-3 gap-y-1.5 pb-2.5 pl-[92px]"
          data-testid="draft-params"
        >
          <Show when={takesCheckpoint()}>
            <label class="flex min-w-0 items-center gap-1.5">
              <span class="shrink-0 font-mono text-xs text-fg-warning">{"{checkpoint}"}</span>
              <input
                value={d().checkpoint}
                onInput={(e) => set({ checkpoint: e.currentTarget.value })}
                placeholder="path on the machine (Checkpoints tab)"
                title={
                  d().checkpoint ||
                  "Absolute path of the checkpoint on the machine; sent from the Checkpoints tab, or pasted"
                }
                aria-label="Checkpoint"
                spellcheck={false}
                data-testid="field-checkpoint"
                class="h-7 w-[28rem] min-w-0 rounded border bg-input px-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker focus:border-line"
                classList={{
                  "border-line-weak": !!d().checkpoint.trim(),
                  "border-line-critical": !d().checkpoint.trim(),
                }}
              />
              <Show when={checkpointOrigin()}>
                <span class="shrink-0 truncate text-xs text-fg-weak" data-testid="checkpoint-origin">
                  {checkpointOrigin()}
                </span>
              </Show>
            </label>
          </Show>
          <For each={params()}>
            {(name) => (
              <label class="flex min-w-0 items-center gap-1.5">
                <span class="shrink-0 font-mono text-xs text-fg-warning">{`{${name}}`}</span>
                <input
                  value={d().params[name] ?? ""}
                  onInput={(e) => set({ params: { ...d().params, [name]: e.currentTarget.value } })}
                  placeholder="empty = nothing"
                  title={
                    paramErrors()[name]
                      ? `Invalid value: ${paramErrors()[name]}`
                      : `Value of {${name}} in the command; split as a shell would ("a b" = a single argument), empty = nothing inserted`
                  }
                  aria-label={`Parameter ${name}`}
                  spellcheck={false}
                  data-testid={`field-param-${name}`}
                  class="h-7 w-56 min-w-0 rounded border bg-input px-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker focus:border-line"
                  classList={{
                    "border-line-weak": !paramErrors()[name],
                    "border-line-critical": !!paramErrors()[name],
                  }}
                />
              </label>
            )}
          </For>
          <Show when={showOverrides}>
            <label class="flex min-w-0 items-center gap-1.5">
              <span class="shrink-0 text-xs text-fg-weak">overrides</span>
              <input
                value={d().overrides}
                onInput={(e) => set({ overrides: e.currentTarget.value })}
                title={
                  overridesError()
                    ? `Invalid overrides: ${overridesError()}`
                    : "Extra arguments of the original run, appended to the command (clear the field to remove them)"
                }
                aria-label="Overrides"
                spellcheck={false}
                data-testid="field-overrides"
                class="h-7 w-72 min-w-0 rounded border bg-input px-2 font-mono text-sm text-fg-strong outline-none focus:border-line"
                classList={{
                  "border-line-weak": !overridesError(),
                  "border-line-critical": !!overridesError(),
                }}
              />
            </label>
          </Show>
        </div>
      </Show>
    </div>
  )
}

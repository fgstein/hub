import { Combobox } from "@kobalte/core/combobox"
import { createEffect, createMemo, on, Show } from "solid-js"
import type { ProjectView } from "~/lib/types"
import {
  commandErrors,
  compatibleVenvs,
  configKey,
  configsCache,
  type Draft,
  draftSelection,
  launchDraft,
  loadConfigs,
  machineReachable,
  removeDraft,
  state,
  updateDraft,
} from "~/stores/app"
import { ConfigPicker } from "./ConfigPicker"
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

/** Une liste avec recherche ; un label fixe si une seule option est possible. */
function Field(props: FieldProps) {
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
              title="Rafraîchir la liste (découverte sur la machine)"
              aria-label="Rafraîchir les configs"
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
          <Combobox.Content class="menu-content z-[70] min-w-[180px] max-w-[420px] rounded-md bg-float p-0.5 shadow-float outline-none">
            <Combobox.Listbox class="max-h-72 overflow-y-auto outline-none" />
            <Show when={props.options.length === 0}>
              <div class="px-2 py-1.5 text-xs text-fg-weak">
                {props.loading ? "chargement…" : "aucune option"}
              </div>
            </Show>
          </Combobox.Content>
        </Combobox.Portal>
      </Combobox>
    </Show>
  )
}

export function DraftRow(props: { project: ProjectView; draft: Draft; autofocus?: boolean }) {
  // État « liste ouverte » mémorisé AVANT que Kobalte ne traite la touche (phase de capture) :
  // Entrée dans une liste ouverte sélectionne une option, elle ne lance pas le run.
  let listOpenAtKeydown = false
  const p = () => props.project
  const d = () => props.draft
  const set = (patch: Partial<Draft>) => updateDraft(p().id, d().key, patch)
  const venvs = createMemo(() => compatibleVenvs(p(), d().image))
  const cfgKey = () => configKey(p().id, p().discover ? d().machine : null)
  const cfg = () => configsCache[cfgKey()]
  const configs = createMemo(() => cfg()?.data?.configs ?? p().static_configs ?? [])

  // Les configs découvertes se chargent quand la machine est choisie.
  createEffect(
    on(cfgKey, () => {
      void loadConfigs(p().id, p().discover ? d().machine : null)
    }),
  )
  // Sélection automatique quand une seule config est disponible.
  createEffect(() => {
    const list = configs()
    if (list.length === 1 && d().config !== list[0]) set({ config: list[0] ?? null })
  })

  const sel = () => draftSelection(p().id, d())
  const blocked = () => commandErrors[p().id] === true
  const canLaunch = () => !!sel() && !blocked() && !d().launching

  const describeMachine = (id: string) => {
    const m = state.inventory?.machines.find((x) => x.id === id)
    const r = machineReachable(id)
    return [m?.label, r === false ? "injoignable" : undefined].filter(Boolean).join(" · ") || undefined
  }
  const describeImage = (id: string) => state.inventory?.images.find((x) => x.id === id)?.image
  const describeVenv = (id: string) => state.inventory?.venvs.find((x) => x.id === id)?.path

  return (
    <div
      class="anim-in flex h-12 items-center gap-3 border-b border-line-weaker px-4"
      classList={{ "opacity-70": d().launching }}
      data-testid="draft-row"
      ref={(el) =>
        el.addEventListener(
          "keydown",
          (e) => {
            listOpenAtKeydown = (e.target as HTMLElement).getAttribute("aria-expanded") === "true"
          },
          true,
        )
      }
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          // Échap ferme d'abord une liste ouverte (gérée par Kobalte), puis le brouillon.
          if (listOpenAtKeydown) return
          e.preventDefault()
          removeDraft(p().id, d().key)
        } else if (e.key === "Enter" && !listOpenAtKeydown) {
          e.preventDefault()
          if (canLaunch()) void launchDraft(p().id, d().key)
        }
      }}
    >
      <StatusDot status={d().launching ? "launching" : "draft"} />
      <span class="w-[68px] shrink-0 text-xs text-fg-weak">{d().launching ? "lancement" : "brouillon"}</span>
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
        <input
          value={d().cuda}
          onInput={(e) => set({ cuda: e.currentTarget.value })}
          placeholder="GPU"
          title="CUDA_VISIBLE_DEVICES (facultatif), ex. 0,1"
          aria-label="CUDA_VISIBLE_DEVICES"
          data-gpu=""
          data-testid="field-gpu"
          class="h-7 min-w-0 rounded border border-line-weak bg-input px-2 font-mono text-sm text-fg-strong outline-none placeholder:font-sans placeholder:text-fg-weaker focus:border-line"
        />
      </div>
      <div class="flex shrink-0 items-center gap-1">
        <button
          type="button"
          class="h-7 rounded-md bg-primary px-3 text-sm font-medium text-primary-fg transition-opacity disabled:opacity-30"
          disabled={!canLaunch()}
          title={
            blocked()
              ? "Commande invalide : {config} manquant"
              : !sel()
                ? "Sélection incomplète"
                : "Lancer (Entrée)"
          }
          onClick={() => void launchDraft(p().id, d().key)}
          data-testid="draft-launch"
        >
          Lancer
        </button>
        <button
          type="button"
          class="flex size-7 items-center justify-center rounded-md text-icon hover:bg-surface-hover hover:text-icon-hover"
          aria-label="Annuler le brouillon (Échap)"
          title="Annuler (Échap)"
          onClick={() => removeDraft(p().id, d().key)}
          data-testid="draft-cancel"
        >
          <IconClose size={12} />
        </button>
      </div>
    </div>
  )
}

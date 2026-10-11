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
          <Combobox.Content
            class="menu-content z-[70] min-w-[180px] max-w-[420px] rounded-md bg-float p-0.5 shadow-float outline-none"
            onCloseAutoFocus={(e) => {
              // Fermée par un clic sur un autre champ : ne pas lui reprendre le focus (sinon
              // la saisie suivante atterrit dans ce champ-ci).
              const active = document.activeElement
              if (active && active !== document.body && !controlEl?.contains(active)) e.preventDefault()
            }}
          >
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

  const overridesError = () => splitArgs(d().overrides).error
  // Le champ d'overrides n'apparaît que pour un brouillon qui en hérite (relance d'un run lancé
  // avec des overrides via l'API, le MCP ou la CLI) : sinon, les paramètres {nom} de la
  // commande remplissent ce rôle.
  const showOverrides = d().overrides.trim() !== ""
  const params = () => p().params ?? []
  const paramErrors = () => draftParamErrors(p().id, d())
  const sel = () => draftSelection(p().id, d())
  const blocked = () => commandErrors[p().id] === true
  const canLaunch = () => !!sel() && !blocked() && !d().launching

  // Brouillon issu de « Relancer » : amené à l'écran et brièvement mis en évidence.
  let rowEl: HTMLDivElement | undefined
  onMount(() => {
    if (!d().focus) return
    rowEl?.scrollIntoView({ block: "center", behavior: "smooth" })
    setTimeout(() => set({ focus: false }), 900)
  })

  const describeMachine = (id: string) => {
    const m = state.inventory?.machines.find((x) => x.id === id)
    const r = machineReachable(id)
    return [m?.label, r === false ? "injoignable" : undefined].filter(Boolean).join(" · ") || undefined
  }
  const describeImage = (id: string) => state.inventory?.images.find((x) => x.id === id)?.image
  const describeVenv = (id: string) =>
    state.inventory?.venvs.find((x) => x.id === id)?.path ??
    (id === "system" ? "sans venv (Python de l'image)" : undefined)

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
      <div class="flex h-12 items-center gap-3">
        <StatusDot status={d().launching ? "launching" : "draft"} />
        <span class="w-[68px] shrink-0 text-xs text-fg-weak">
          {d().launching ? "lancement" : "brouillon"}
        </span>
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
                : overridesError()
                  ? `Overrides invalides : ${overridesError()}`
                  : Object.keys(paramErrors()).length
                    ? `Paramètre invalide : {${Object.keys(paramErrors())[0]}}`
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
      <Show when={params().length || showOverrides}>
        <div
          class="-mt-1 flex flex-wrap items-center gap-x-3 gap-y-1.5 pb-2.5 pl-[92px]"
          data-testid="draft-params"
        >
          <For each={params()}>
            {(name) => (
              <label class="flex min-w-0 items-center gap-1.5">
                <span class="shrink-0 font-mono text-xs text-fg-warning">{`{${name}}`}</span>
                <input
                  value={d().params[name] ?? ""}
                  onInput={(e) => set({ params: { ...d().params, [name]: e.currentTarget.value } })}
                  placeholder="vide = rien"
                  title={
                    paramErrors()[name]
                      ? `Valeur invalide : ${paramErrors()[name]}`
                      : `Valeur de {${name}} dans la commande ; découpée comme par un shell ("a b" = un seul argument), vide = rien d'inséré`
                  }
                  aria-label={`Paramètre ${name}`}
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
                    ? `Overrides invalides : ${overridesError()}`
                    : "Overrides Hydra du run d'origine, ajoutés à la fin de la commande (videz le champ pour les retirer)"
                }
                aria-label="Overrides Hydra"
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

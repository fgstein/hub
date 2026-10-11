import { createEffect, createSignal, For, on, onCleanup, Show } from "solid-js"
import { api } from "~/lib/api"
import { segments, validateCommand } from "~/lib/command"
import type { PreviewResult, ProjectView, Selection } from "~/lib/types"
import { draftSelection, resetCommand, saveCommand, setCommandErrors, state } from "~/stores/app"
import { toast } from "~/stores/toasts"
import { IconCheck, IconChevronDown, IconCopy, IconReset, IconSpinner } from "./Icons"

/** Champ de la commande générique d'un projet, avec aperçu de la commande complète. */
export function CommandEditor(props: { project: ProjectView }) {
  const [text, setText] = createSignal(props.project.command)
  const [saved, setSaved] = createSignal<"idle" | "saving" | "saved">("idle")
  const [showPreview, setShowPreview] = createSignal(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let savedTimer: ReturnType<typeof setTimeout> | undefined
  let textarea!: HTMLTextAreaElement
  const error = () => validateCommand(text())
  createEffect(() => setCommandErrors(props.project.id, !!error()))
  onCleanup(() => setCommandErrors(props.project.id, false))

  // Mise à jour externe (autre onglet, MCP, retour au YAML) quand l'utilisateur ne tape pas.
  createEffect(
    on(
      () => props.project.command,
      (cmd) => {
        if (timer === undefined && cmd !== text()) setText(cmd)
      },
    ),
  )

  const autosize = () => {
    if (!textarea) return
    textarea.style.height = "0px"
    textarea.style.height = `${textarea.scrollHeight}px`
  }
  createEffect(on(text, () => queueMicrotask(autosize)))

  const schedule = (value: string) => {
    setText(value)
    clearTimeout(timer)
    timer = setTimeout(async () => {
      timer = undefined
      if (validateCommand(value) || value === props.project.command) return
      setSaved("saving")
      const ok = await saveCommand(props.project.id, value)
      setSaved(ok ? "saved" : "idle")
      clearTimeout(savedTimer)
      savedTimer = setTimeout(() => setSaved("idle"), 1800)
    }, 500)
  }
  onCleanup(() => {
    clearTimeout(timer)
    clearTimeout(savedTimer)
  })

  const reset = async () => {
    clearTimeout(timer)
    timer = undefined
    const view = await resetCommand(props.project.id)
    if (view) setText(view.command)
  }

  return (
    <section class="border-b border-line-weak" data-testid="command-editor">
      <div class="flex items-start gap-2 px-4 py-2.5">
        <span class="select-none pt-[3px] font-mono text-sm text-fg-weaker">$</span>
        <div
          class="relative min-w-0 flex-1 rounded border px-2 py-[3px] font-mono text-sm leading-5 transition-colors"
          classList={{
            "border-transparent hover:border-line-weak focus-within:border-line": !error(),
            "border-line-critical": !!error(),
          }}
        >
          <pre
            aria-hidden="true"
            class="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-all px-2 py-[3px] text-fg-strong"
          >
            <For each={segments(text())}>
              {(s) => (
                <span
                  classList={{
                    "text-placeholder": s.kind === "placeholder",
                    "text-fg-critical underline decoration-wavy": s.kind === "unknown",
                  }}
                >
                  {s.text}
                </span>
              )}
            </For>
            {"​"}
          </pre>
          <textarea
            ref={textarea}
            rows={1}
            spellcheck={false}
            aria-label="Commande générique"
            aria-invalid={!!error()}
            data-testid="command-input"
            value={text()}
            onInput={(e) => schedule(e.currentTarget.value.replace(/\n/g, " "))}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.preventDefault()
            }}
            class="relative block w-full resize-none overflow-hidden whitespace-pre-wrap break-all bg-transparent text-transparent caret-[var(--text-strong)] outline-none selection:text-transparent"
          />
        </div>
        <div class="flex shrink-0 items-center gap-1 pt-[2px]">
          <span class="flex w-[86px] items-center justify-end gap-1 text-xs text-fg-weak" aria-live="polite">
            <Show when={saved() === "saving"}>
              <IconSpinner size={11} />
            </Show>
            <Show when={saved() === "saved"}>
              <IconCheck size={11} class="text-fg-success" />
              <span data-testid="command-saved">enregistré</span>
            </Show>
          </span>
          <button
            type="button"
            class="flex size-6 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover disabled:opacity-30"
            title="Revenir à la commande du YAML"
            aria-label="Revenir à la commande du YAML"
            disabled={!props.project.command_overridden && text() === props.project.yaml_command}
            onClick={reset}
            data-testid="command-reset"
          >
            <IconReset />
          </button>
          <button
            type="button"
            class="flex h-6 items-center gap-1 rounded px-1.5 text-xs text-fg hover:bg-surface-hover hover:text-fg-strong"
            aria-expanded={showPreview()}
            onClick={() => setShowPreview((v) => !v)}
            data-testid="preview-toggle"
          >
            <IconChevronDown size={12} class={`transition-transform ${showPreview() ? "rotate-180" : ""}`} />
            aperçu
          </button>
        </div>
      </div>
      <Show when={error()}>
        <div class="-mt-1.5 px-9 pb-2 text-xs text-fg-critical" data-testid="command-error">
          {error()} — lancement désactivé
        </div>
      </Show>
      <div class="th-collapse" data-open={showPreview() ? "" : undefined}>
        <div>
          <Show when={showPreview()}>
            <Preview project={props.project} command={text()} />
          </Show>
        </div>
      </div>
    </section>
  )
}

/** Sélection utilisée pour l'aperçu : premier brouillon complet, sinon dernière sélection. */
function previewSelection(p: ProjectView): Selection | null {
  for (const d of state.drafts[p.id] ?? []) {
    const sel = draftSelection(p.id, d)
    if (sel) return sel
  }
  const last = state.ui.last_selection?.[p.id]
  const machine = last?.machine && p.machines.includes(last.machine) ? last.machine : p.machines[0]
  const image = last?.image && p.images.includes(last.image) ? last.image : p.images[0]
  const venv = last?.venv && p.venvs.includes(last.venv) ? last.venv : p.venvs[0]
  const config = last?.config ?? p.static_configs?.[0] ?? "<config>"
  if (!machine || !image || !venv) return null
  return {
    project: p.id,
    machine,
    image,
    venv,
    config,
    overrides: [],
    cuda_visible_devices: last?.cuda_visible_devices ?? null,
  }
}

function Preview(props: { project: ProjectView; command: string }) {
  const [result, setResult] = createSignal<PreviewResult | null>(null)
  const [err, setErr] = createSignal<string | null>(null)
  const [loading, setLoading] = createSignal(false)

  createEffect(
    on(
      () => [JSON.stringify(previewSelection(props.project)), props.project.command] as const,
      async ([selJson]) => {
        const sel = JSON.parse(selJson) as Selection | null
        if (!sel) {
          setErr("aucune machine, image ou venv disponible pour ce projet")
          return
        }
        setLoading(true)
        try {
          setResult(await api.preview(sel))
          setErr(null)
        } catch (e) {
          setErr(e instanceof Error ? e.message : String(e))
        } finally {
          setLoading(false)
        }
      },
    ),
  )

  const copy = async () => {
    const r = result()
    if (!r) return
    try {
      await navigator.clipboard.writeText(r.exact)
      toast("commande copiée", "success")
    } catch {
      toast("copie impossible (presse-papier indisponible)", "error")
    }
  }

  return (
    <div class="relative mx-4 mb-3 rounded-md border border-line-weak bg-inset" data-testid="preview">
      <button
        type="button"
        class="absolute right-1.5 top-1.5 flex size-6 items-center justify-center rounded text-icon hover:bg-surface-hover hover:text-icon-hover"
        onClick={copy}
        title="Copier la commande exacte"
        aria-label="Copier la commande"
      >
        <IconCopy />
      </button>
      <Show when={!err()} fallback={<div class="px-3 py-2 text-xs text-fg-critical">{err()}</div>}>
        <pre
          class="overflow-x-auto px-3 py-2 pr-10 font-mono text-xs leading-5 text-fg"
          classList={{ "opacity-60": loading() }}
        >
          {result()?.text ?? "…"}
        </pre>
      </Show>
    </div>
  )
}

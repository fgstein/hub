import { createMemo, createSignal, For, Show } from "solid-js"
import type { ProjectView } from "~/lib/types"
import {
  activeExternalIds,
  activeRunIds,
  addDraft,
  finishedExternalIds,
  finishedRunIds,
  state,
} from "~/stores/app"
import { CommandEditor } from "./CommandEditor"
import { DraftRow } from "./DraftRow"
import { ExternalRunRow } from "./ExternalRunRow"
import { IconChevronRight, IconPlus } from "./Icons"
import { RunRow } from "./RunRow"

export function ProjectPage(props: { project: ProjectView }) {
  const id = () => props.project.id
  const active = createMemo(() => activeRunIds(id()))
  const finished = createMemo(() => finishedRunIds(id()))
  const activeExternal = createMemo(() => activeExternalIds(id()))
  const finishedExternal = createMemo(() => finishedExternalIds(id()))
  const drafts = () => state.drafts[id()] ?? []
  const [showFinished, setShowFinished] = createSignal(true)
  const finishedCount = () => finished().length + finishedExternal().length
  const empty = () => active().length === 0 && activeExternal().length === 0 && drafts().length === 0

  return (
    <div class="flex min-h-0 flex-1 flex-col overflow-y-auto" data-testid="project-page" data-project={id()}>
      <CommandEditor project={props.project} />
      <section aria-label="Active runs" data-testid="active-runs">
        <For each={active()}>{(rid) => <RunRow id={rid} />}</For>
        <For each={activeExternal()}>{(rid) => <ExternalRunRow id={rid} />}</For>
        <For each={drafts()}>
          {(d, i) => (
            <DraftRow
              project={props.project}
              draft={d}
              autofocus={i() === drafts().length - 1 && !d.noAutofocus}
            />
          )}
        </For>
      </section>
      <Show
        when={!empty()}
        fallback={
          <div class="flex flex-col items-center gap-3 px-4 py-14 text-center" data-testid="empty-state">
            <p class="text-sm text-fg-weak">No active run in this project.</p>
            <button
              type="button"
              class="flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-sm font-medium text-primary-fg"
              onClick={() => addDraft(id())}
              data-testid="add-draft"
            >
              <IconPlus size={12} /> New run
              <kbd class="ml-1 rounded border border-current/30 px-1 font-mono text-[10px] opacity-60">N</kbd>
            </button>
          </div>
        }
      >
        <div class="flex justify-center py-2">
          <button
            type="button"
            class="flex size-7 items-center justify-center rounded-md border border-line-weak text-icon transition-colors hover:border-line hover:bg-surface-hover hover:text-icon-hover"
            onClick={() => addDraft(id())}
            title="New run (N)"
            aria-label="New run"
            data-testid="add-draft"
          >
            <IconPlus size={12} />
          </button>
        </div>
      </Show>
      <Show when={finishedCount()}>
        <section
          class="mt-2 border-t border-line-weak"
          aria-label="Finished runs"
          data-testid="finished-runs"
        >
          <button
            type="button"
            class="flex h-9 w-full items-center gap-1.5 px-4 text-xs font-medium text-fg-weak hover:text-fg-strong"
            aria-expanded={showFinished()}
            onClick={() => setShowFinished((v) => !v)}
            data-testid="finished-toggle"
          >
            <IconChevronRight
              size={11}
              class={`transition-transform duration-150 ${showFinished() ? "rotate-90" : ""}`}
            />
            Finished ({finishedCount()})
          </button>
          <div class="th-collapse" data-open={showFinished() ? "" : undefined}>
            <div>
              <For each={finished()}>{(rid) => <RunRow id={rid} />}</For>
              <For each={finishedExternal()}>{(rid) => <ExternalRunRow id={rid} />}</For>
            </div>
          </div>
        </section>
      </Show>
    </div>
  )
}

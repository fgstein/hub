/**
 * Lookin links of an MLflow run, as one blue pill: "Lookin" (opens Lookin ready to put the run in
 * a workspace) ‖ the workspaces using the run, each a link; past MAX, "+n" opens a menu listing
 * the others, each a link too. Nothing shows without a lookin section in the inventory.
 */
import { DropdownMenu } from "@kobalte/core/dropdown-menu"
import { For, onMount, Show } from "solid-js"
import { lookin, lookinUrl, lookinWorkspaces, newWorkspaceUrl, watchLookin } from "~/stores/lookin"

const stop = (e: Event) => e.stopPropagation()

/** Workspaces named in the pill; the others are in the "+n" menu. */
const MAX = 2

export function LookinLinks(props: {
  mlflowRunId: string | null | undefined
  /** MLflow server of the run, as TrainHub knows it (a hint for Lookin). */
  trackingUri?: string | null
  /** "Lookin" opens Lookin to put the run in a workspace (inferences); otherwise it is a label. */
  button?: boolean
  /** Workspaces shown at most (the others are in the "+n" menu). */
  max?: number
}) {
  onMount(watchLookin)
  const workspaces = () => lookinWorkspaces(props.mlflowRunId)
  const max = () => props.max ?? MAX
  const shown = () => workspaces().slice(0, max())
  const hidden = () => workspaces().slice(max())
  const seg = "px-1.5 hover:bg-st-blue/15 hover:text-fg-strong"
  return (
    <Show when={lookinUrl() && props.mlflowRunId && (props.button || workspaces().length)}>
      {/* One blue pill: Lookin ‖ workspace 1 | workspace 2 */}
      <span
        class="flex h-5 min-w-0 shrink items-center overflow-hidden rounded-full border border-st-blue/60 bg-st-blue/10 text-xs leading-4 text-st-blue"
        data-testid="lookin-links"
      >
        <Show
          when={props.button}
          fallback={
            <span class="shrink-0 px-1.5 font-medium" title="Lookin workspaces using this MLflow run">
              Lookin
            </span>
          }
        >
          <a
            href={newWorkspaceUrl(lookinUrl()!, props.mlflowRunId!, props.trackingUri)}
            target="_blank"
            rel="noopener noreferrer"
            class={`flex h-full shrink-0 items-center font-medium ${seg}`}
            title={
              lookin.data?.error
                ? `Open in Lookin, in a new or existing workspace (${lookin.data.error})`
                : "Open in Lookin, in a new or existing workspace"
            }
            onClick={stop}
            onKeyDown={stop}
            data-testid="lookin-open"
          >
            Lookin
          </a>
        </Show>
        <Show when={workspaces().length}>
          <span class="shrink-0 text-st-blue/70" aria-hidden="true">
            ‖
          </span>
        </Show>
        <For each={shown()}>
          {(w, i) => (
            <>
              <Show when={i() > 0}>
                <span class="shrink-0 text-st-blue/50" aria-hidden="true">
                  |
                </span>
              </Show>
              <a
                href={w.url}
                target="_blank"
                rel="noopener noreferrer"
                class={`flex h-full min-w-0 max-w-40 items-center ${seg}`}
                title={`Lookin workspace ${w.workspace}${w.group ? ` (${w.group})` : ""}, experiment ${w.experiment}: open it`}
                onClick={stop}
                onKeyDown={stop}
                data-testid="lookin-workspace"
              >
                <span class="truncate">{w.workspace}</span>
              </a>
            </>
          )}
        </For>
        <Show when={hidden().length}>
          <DropdownMenu placement="bottom-end" gutter={4}>
            <DropdownMenu.Trigger
              class={`flex h-full shrink-0 items-center pr-1.5 pl-1 text-st-blue ${seg}`}
              title={`${hidden().length} more Lookin workspace${hidden().length > 1 ? "s" : ""}: open one`}
              onClick={stop}
              onKeyDown={stop}
              data-testid="lookin-more"
            >
              +{hidden().length}
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                class="menu-content z-[70] min-w-[200px] max-w-[420px] rounded-md bg-float p-0.5 shadow-float outline-none"
                data-testid="lookin-more-menu"
              >
                <For each={hidden()}>
                  {(w) => (
                    <DropdownMenu.Item
                      onSelect={() => window.open(w.url, "_blank", "noopener")}
                      class="flex h-7 cursor-pointer items-center gap-2 rounded px-2.5 text-sm text-st-blue outline-none data-[highlighted]:bg-surface-active"
                      title={`Lookin workspace ${w.workspace}${w.group ? ` (${w.group})` : ""}, experiment ${w.experiment}: open it`}
                      data-testid="lookin-more-workspace"
                    >
                      <span class="truncate">{w.workspace}</span>
                      <Show when={w.group}>
                        <span class="ml-auto shrink-0 text-xs text-fg-weak">{w.group}</span>
                      </Show>
                    </DropdownMenu.Item>
                  )}
                </For>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu>
        </Show>
      </span>
    </Show>
  )
}

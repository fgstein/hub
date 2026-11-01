/**
 * Lookin links of an MLflow run: one badge per Lookin workspace using it (a click opens the
 * workspace), and the "Lookin" button that opens Lookin ready to put the run in a workspace.
 * Nothing shows without a lookin section in the inventory.
 */
import { For, onMount, Show } from "solid-js"
import { lookin, lookinUrl, lookinWorkspaces, newWorkspaceUrl, watchLookin } from "~/stores/lookin"

const stop = (e: Event) => e.stopPropagation()

export function LookinLinks(props: {
  mlflowRunId: string | null | undefined
  /** MLflow server of the run, as TrainHub knows it (a hint for Lookin). */
  trackingUri?: string | null
  /** "Lookin" opens Lookin to put the run in a workspace (inferences); otherwise it is a label. */
  button?: boolean
  /** Workspaces shown at most (the others are counted in a "+n"). */
  max?: number
}) {
  onMount(watchLookin)
  const workspaces = () => lookinWorkspaces(props.mlflowRunId)
  const max = () => props.max ?? 3
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
          <span
            class="shrink-0 pr-1.5 pl-1 text-st-blue/80"
            title={`Also in Lookin: ${hidden()
              .map((w) => w.workspace)
              .join(", ")}`}
            data-testid="lookin-more"
          >
            +{hidden().length}
          </span>
        </Show>
      </span>
    </Show>
  )
}

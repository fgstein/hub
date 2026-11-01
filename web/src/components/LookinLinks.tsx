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
  /** Show the "Lookin" button (inferences); the badges show whenever a workspace uses the run. */
  button?: boolean
  /** Badges shown at most (the others are counted in a "+n"). */
  max?: number
}) {
  onMount(watchLookin)
  const workspaces = () => lookinWorkspaces(props.mlflowRunId)
  const max = () => props.max ?? 3
  const shown = () => workspaces().slice(0, max())
  const hidden = () => workspaces().slice(max())
  return (
    <Show when={lookinUrl() && props.mlflowRunId}>
      <span class="flex min-w-0 shrink items-center gap-1" data-testid="lookin-links">
        <For each={shown()}>
          {(w) => (
            <a
              href={w.url}
              target="_blank"
              rel="noopener noreferrer"
              class="max-w-40 shrink truncate rounded-full border border-line-weak px-1.5 text-xs leading-4 text-fg hover:border-line hover:bg-surface-hover hover:text-fg-strong"
              title={`Lookin workspace ${w.workspace}${w.group ? ` (${w.group})` : ""}, experiment ${w.experiment}: open it`}
              onClick={stop}
              onKeyDown={stop}
              data-testid="lookin-workspace"
            >
              {w.workspace}
            </a>
          )}
        </For>
        <Show when={hidden().length}>
          <span
            class="shrink-0 text-xs text-fg-weak"
            title={`Also in Lookin: ${hidden()
              .map((w) => w.workspace)
              .join(", ")}`}
            data-testid="lookin-more"
          >
            +{hidden().length}
          </span>
        </Show>
        <Show when={props.button}>
          <a
            href={newWorkspaceUrl(lookinUrl()!, props.mlflowRunId!, props.trackingUri)}
            target="_blank"
            rel="noopener noreferrer"
            class="flex h-5 shrink-0 items-center rounded bg-surface px-1.5 text-xs text-fg-weak hover:bg-surface-hover hover:text-fg-strong"
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
      </span>
    </Show>
  )
}

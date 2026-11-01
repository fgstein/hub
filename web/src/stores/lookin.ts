/**
 * Lookin: workspaces per MLflow run id, read from the server (which asks Lookin and caches
 * the answer 30 s). Loaded when a page draws a Lookin badge, then refreshed every 30 s while
 * the window is visible. The MLflow run id is the whole link: nothing is tagged.
 */
import { createStore } from "solid-js/store"
import { api } from "~/lib/api"
import type { LookinIndex, LookinWorkspace } from "~/lib/types"
import { state } from "./app"

const REFRESH_MS = 30_000

const [lookin, setLookin] = createStore<{ loading: boolean; data?: LookinIndex; at: number }>({
  loading: false,
  at: 0,
})

export { lookin }

/** Lookin, as the browser reaches it (``null``: no lookin section in the inventory). */
export const lookinUrl = (): string | null => state.inventory?.lookin_url ?? null

let timer: ReturnType<typeof setInterval> | undefined

/** Ask the server (``refresh``: it asks Lookin again now, bypassing its cache). */
export async function loadLookin(refresh = false): Promise<void> {
  if (lookin.loading || !lookinUrl()) return
  setLookin("loading", true)
  try {
    setLookin({ loading: false, data: await api.lookin(refresh), at: Date.now() })
  } catch {
    // An unreachable TrainHub is shown elsewhere; Lookin's own errors arrive in data.error.
    setLookin({ loading: false, at: Date.now() })
  }
}

/** Load once, then keep fresh while the window is visible (idempotent). */
export function watchLookin(): void {
  if (!lookinUrl()) return
  if (Date.now() - lookin.at > REFRESH_MS) void loadLookin()
  if (timer !== undefined) return
  timer = setInterval(() => {
    if (document.visibilityState === "visible") void loadLookin()
  }, REFRESH_MS)
}

/** Lookin workspaces with an experiment made from this MLflow run. */
export function lookinWorkspaces(mlflowRunId: string | null | undefined): LookinWorkspace[] {
  if (!mlflowRunId) return []
  return lookin.data?.runs?.[mlflowRunId] ?? []
}

/** Lookin page offering to put the MLflow run in a workspace (new or existing). */
export function newWorkspaceUrl(base: string, mlflowRunId: string, trackingUri?: string | null): string {
  const q = new URLSearchParams({ mlflow_run: mlflowRunId })
  if (trackingUri) q.set("mlflow_server", trackingUri)
  return `${base}/?${q}`
}

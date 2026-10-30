/** Checkpoints tab: listings per source (loaded on demand) and deletion. */
import { createStore } from "solid-js/store"
import { api } from "~/lib/api"
import type { CheckpointDeleteResult, CheckpointListing, CheckpointSourceView } from "~/lib/types"
import { toastError } from "./toasts"

interface ListingState {
  loading: boolean
  data?: CheckpointListing
  error?: string
}

const [listings, setListings] = createStore<Record<string, ListingState>>({})

export { listings }

/**
 * Load a source listing. ``cached``: only if not loaded yet; ``reload``: ask the server again
 * (its scan cache); ``rescan``: the server scans the disk again. The current listing stays
 * shown meanwhile.
 */
export async function loadCheckpoints(
  source: string,
  mode: "cached" | "reload" | "rescan" = "cached",
): Promise<void> {
  const cur = listings[source]
  if (cur?.loading || (cur?.data && mode === "cached")) return
  setListings(source, { loading: true, data: cur?.data, error: undefined })
  try {
    const data = await api.checkpoints(source, mode === "rescan")
    setListings(source, { loading: false, data, error: data.error ?? undefined })
  } catch (e) {
    setListings(source, {
      loading: false,
      data: cur?.data,
      error: e instanceof Error ? e.message : String(e),
    })
  }
}

/** ``checkpoints.updated``: another client (or an agent) changed the source; reload a loaded
 * listing whose totals no longer match. */
export function onSourceUpdated(src: CheckpointSourceView): void {
  const cur = listings[src.id]
  if (!cur?.data || cur.loading) return
  if (src.count !== cur.data.count || src.size !== cur.data.size) void loadCheckpoints(src.id, "reload")
}

export async function deleteCheckpoints(
  source: string,
  paths: string[],
): Promise<CheckpointDeleteResult | null> {
  try {
    const res = await api.deleteCheckpoints(source, paths)
    // The server updated its cache: reload without rescanning the disk.
    await loadCheckpoints(source, "reload")
    return res
  } catch (e) {
    toastError(e)
    return null
  }
}

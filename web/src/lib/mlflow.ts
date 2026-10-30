import type { RunStatus, RunView } from "./types"

/** Training state read from MLflow, shown by the color of the status dot. */
export type Training = "healthy" | "failed" | "finished"

/**
 * Training state of a run from its MLflow run (``null``: no MLflow information, the dot keeps
 * its docker status color). Statuses about the container itself (launching, paused,
 * stopping, stopped, unreachable) are never overridden.
 *
 * - running: MLflow FAILED/KILLED → failed (red), FINISHED → finished (blue), else healthy (green)
 * - succeeded: MLflow FAILED/KILLED → failed (red), else finished (blue)
 * - failed: failed (red)
 */
export function trainingState(status: RunStatus, mlflowStatus: string | null | undefined): Training | null {
  if (!mlflowStatus) return null
  const mlFailed = mlflowStatus === "FAILED" || mlflowStatus === "KILLED"
  switch (status) {
    case "running":
      return mlFailed ? "failed" : mlflowStatus === "FINISHED" ? "finished" : "healthy"
    case "succeeded":
      return mlFailed ? "failed" : "finished"
    case "failed":
      return "failed"
    default:
      return null
  }
}

export interface Progress {
  epoch: number | null
  max: number | null
  /** Between 0 and 1; ``null`` if unknown (no maximum epoch). */
  fraction: number | null
}

/** Progress of a run from its MLflow epochs (``null``: nothing to show). */
export function progress(run: Pick<RunView, "mlflow">): Progress | null {
  const ml = run.mlflow
  if (!ml?.run_id) return null
  const epoch = ml.epoch ?? null
  const max = ml.max_epochs ?? null
  if (epoch == null && max == null) return null
  const fraction = max && max > 0 ? Math.max(0, Math.min(1, (epoch ?? 0) / max)) : null
  return { epoch, max, fraction }
}

/** ``3``, ``2.5``: an epoch without useless decimals. */
export const formatEpoch = (n: number | null) => (n == null ? "?" : String(Math.round(n * 10) / 10))

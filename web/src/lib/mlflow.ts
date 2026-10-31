import type { MlflowInfo, RunStatus } from "./types"

/** Training state read from MLflow, shown by the color of the status dot. */
export type Training = "healthy" | "stale" | "failed" | "finished"

/**
 * Training state of a run from its MLflow run (``null``: no MLflow information, the dot keeps
 * its docker status color). Statuses about the container itself (launching, paused,
 * stopping, stopped, unreachable) are never overridden.
 *
 * - running: MLflow FAILED/KILLED → failed (red), FINISHED → finished (blue), running with no
 *   new metric for a while (``stale``) → stale (orange), else healthy (green)
 * - succeeded: MLflow FAILED/KILLED → failed (red), else finished (blue)
 * - failed: failed (red)
 */
export function trainingState(
  status: RunStatus,
  mlflowStatus: string | null | undefined,
  stale = false,
): Training | null {
  if (!mlflowStatus) return null
  const mlFailed = mlflowStatus === "FAILED" || mlflowStatus === "KILLED"
  switch (status) {
    case "running":
      if (mlFailed) return "failed"
      if (mlflowStatus === "FINISHED") return "finished"
      return stale ? "stale" : "healthy"
    case "succeeded":
      return mlFailed ? "failed" : "finished"
    case "failed":
      return "failed"
    default:
      return null
  }
}

/** Training state of an external run, from its MLflow status alone. */
export function externalTraining(mlflowStatus: string | null | undefined, stale = false): Training {
  if (mlflowStatus === "FAILED" || mlflowStatus === "KILLED") return "failed"
  if (mlflowStatus === "FINISHED") return "finished"
  return stale ? "stale" : "healthy"
}

export interface Progress {
  epoch: number | null
  max: number | null
  /** Between 0 and 1; ``null`` if unknown (no maximum epoch). */
  fraction: number | null
}

/** Progress of a run from its MLflow epochs (``null``: nothing to show). */
export function progress(run: {
  mlflow?: Pick<MlflowInfo, "run_id" | "epoch" | "max_epochs"> | null
}): Progress | null {
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

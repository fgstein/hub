/** Helpers of the GPU picker: CUDA_VISIBLE_DEVICES ↔ indices, memory formatting. */

/** Indices of a CUDA_VISIBLE_DEVICES value (``null``: no restriction, every GPU visible). */
export function parseIndices(cuda: string): number[] | null {
  const v = cuda.replace(/\s/g, "")
  if (v === "" || v === "all") return null
  return v
    .split(",")
    .filter((p) => /^\d+$/.test(p))
    .map(Number)
}

/** Add or remove a GPU from a CUDA_VISIBLE_DEVICES value (sorted, without duplicates). */
export function toggleIndex(cuda: string, index: number): string {
  const cur = new Set(parseIndices(cuda) ?? [])
  if (cur.has(index)) cur.delete(index)
  else cur.add(index)
  return [...cur].sort((a, b) => a - b).join(",")
}

/** MiB → “39.2 GiB” (one decimal under 100 GiB). */
export function formatGiB(mib: number | null | undefined): string {
  if (mib == null) return "?"
  const gib = mib / 1024
  return gib < 100 ? gib.toFixed(1) : String(Math.round(gib))
}

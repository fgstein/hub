import { createStore, produce } from "solid-js/store"

export interface Toast {
  id: number
  kind: "error" | "info" | "success"
  message: string
}

const [toasts, setToasts] = createStore<Toast[]>([])
let next = 1

export { toasts }

export function dismiss(id: number): void {
  setToasts((list) => list.filter((t) => t.id !== id))
}

export function toast(message: string, kind: Toast["kind"] = "info", ttl = kind === "error" ? 6000 : 3000) {
  const id = next++
  setToasts(
    produce((list) => {
      list.push({ id, kind, message })
      if (list.length > 5) list.shift()
    }),
  )
  setTimeout(() => dismiss(id), ttl)
  return id
}

export const toastError = (e: unknown) => toast(e instanceof Error ? e.message : String(e), "error")

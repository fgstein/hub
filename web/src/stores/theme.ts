import { createRoot, createSignal } from "solid-js"

export type ThemePref = "system" | "dark" | "light"
const KEY = "trainhub.theme"

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY)
    if (v === "dark" || v === "light" || v === "system") return v
  } catch {
    /* storage unavailable */
  }
  return "dark"
}

const theme = createRoot(() => {
  const media = window.matchMedia?.("(prefers-color-scheme: dark)")
  const [pref, setPref] = createSignal<ThemePref>(read())
  const [systemDark, setSystemDark] = createSignal(media?.matches ?? true)
  media?.addEventListener("change", (e) => setSystemDark(e.matches))
  const resolved = () => (pref() === "system" ? (systemDark() ? "dark" : "light") : pref())
  return { pref, setPref, resolved }
})

export const themePref = theme.pref
/** Effective theme: "dark" or "light". */
export const resolvedTheme = theme.resolved

export function applyTheme(): void {
  document.documentElement.dataset.theme = theme.resolved()
}

export function cycleTheme(): void {
  const order: ThemePref[] = ["dark", "light", "system"]
  const next = order[(order.indexOf(theme.pref()) + 1) % order.length] ?? "system"
  theme.setPref(next)
  try {
    localStorage.setItem(KEY, next)
  } catch {
    /* ignore */
  }
}

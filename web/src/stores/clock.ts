import { createRoot, createSignal } from "solid-js"

/** Shared clock (one update per second), corrected for the skew with the server. */
const clock = createRoot(() => {
  const [now, setNow] = createSignal(Date.now())
  const [skew, setSkew] = createSignal(0)
  setInterval(() => setNow(Date.now()), 1000)
  return { now: () => now() + skew(), setSkew }
})

export const now = clock.now
export const setServerTime = (iso: string) => clock.setSkew(Date.parse(iso) - Date.now())

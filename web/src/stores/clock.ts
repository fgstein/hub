import { createRoot, createSignal } from "solid-js"

/** Horloge partagée (une mise à jour par seconde), corrigée du décalage avec le serveur. */
const clock = createRoot(() => {
  const [now, setNow] = createSignal(Date.now())
  const [skew, setSkew] = createSignal(0)
  setInterval(() => setNow(Date.now()), 1000)
  return { now: () => now() + skew(), setSkew }
})

export const now = clock.now
export const setServerTime = (iso: string) => clock.setSkew(Date.parse(iso) - Date.now())

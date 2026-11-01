/**
 * Deep links (``?run=`` of a project, ``?run=&inference=`` of a checkpoint source): bring the
 * element into view and flash it, once it is drawn. Used by the links Lookin hands out.
 */
const TRIES = 20
const EVERY_MS = 100

export function revealAndFlash(selector: string): void {
  let n = 0
  const tick = () => {
    const el = document.querySelector<HTMLElement>(selector)
    if (!el) {
      if (++n < TRIES) setTimeout(tick, EVERY_MS)
      return
    }
    el.scrollIntoView({ block: "center" })
    el.animate?.([{ backgroundColor: "var(--color-surface-active)" }, { backgroundColor: "transparent" }], {
      duration: 2500,
      easing: "ease-out",
    })
  }
  setTimeout(tick, 0)
}

/** ``[name="value"]`` with the value escaped. */
export const attr = (name: string, value: string) => `[${name}="${CSS.escape(value)}"]`

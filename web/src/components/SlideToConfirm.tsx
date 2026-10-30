import { createSignal, Show } from "solid-js"
import { IconChevronRight, IconSpinner } from "./Icons"

const HANDLE = 32
const KEY_STEP = 0.1

/**
 * Confirmation by a deliberate gesture: drag the handle to the end of the track. Released
 * before the end, it goes back. Keyboard: arrows move it by steps, reaching the end confirms.
 */
export function SlideToConfirm(props: {
  label: string
  onConfirm: () => void
  disabled?: boolean
  busy?: boolean
  testid?: string
}) {
  let track: HTMLDivElement | undefined
  const [offset, setOffset] = createSignal(0)
  const [dragging, setDragging] = createSignal(false)
  let startX = 0
  let startOffset = 0

  const max = () => Math.max(1, (track?.clientWidth ?? 0) - HANDLE - 4)
  const ratio = () => Math.min(1, offset() / max())
  const locked = () => !!props.disabled || !!props.busy

  const confirm = () => {
    setOffset(max())
    props.onConfirm()
    // back to the start once the action has been handed over
    setTimeout(() => setOffset(0), 400)
  }

  const onDown = (e: PointerEvent) => {
    if (locked()) return
    e.preventDefault()
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    startX = e.clientX
    startOffset = offset()
    setDragging(true)
  }
  const onMove = (e: PointerEvent) => {
    if (!dragging()) return
    setOffset(Math.max(0, Math.min(max(), startOffset + e.clientX - startX)))
  }
  const onUp = () => {
    if (!dragging()) return
    setDragging(false)
    if (offset() >= max() - 1) confirm()
    else setOffset(0)
  }
  const onKey = (e: KeyboardEvent) => {
    if (locked()) return
    let next: number | null = null
    if (e.key === "ArrowRight") next = offset() + max() * KEY_STEP
    else if (e.key === "ArrowLeft") next = offset() - max() * KEY_STEP
    else if (e.key === "Home") next = 0
    if (next === null) return
    e.preventDefault()
    e.stopPropagation()
    next = Math.max(0, Math.min(max(), next))
    if (next >= max() - 1) confirm()
    else setOffset(next)
  }

  return (
    <div
      ref={track}
      class="relative h-9 w-72 shrink-0 select-none overflow-hidden rounded-md border border-line-critical bg-critical-bg"
      classList={{ "opacity-40": !!props.disabled }}
      data-testid={props.testid}
    >
      <div
        class="absolute inset-y-0 left-0 bg-fg-critical/25"
        classList={{ "transition-[width] duration-200": !dragging() }}
        style={{ width: `${offset() + HANDLE + 2}px` }}
      />
      <span
        class="pointer-events-none absolute inset-0 flex items-center justify-center pl-8 text-xs font-medium text-fg-critical"
        style={{ opacity: String(1 - ratio() * 0.8) }}
      >
        {props.busy ? "deleting…" : props.label}
      </span>
      <div
        role="slider"
        tabIndex={locked() ? -1 : 0}
        aria-label={props.label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(ratio() * 100)}
        aria-disabled={locked()}
        class="absolute top-0.5 left-0.5 flex h-[30px] items-center justify-center rounded bg-fg-critical text-bg outline-none focus-visible:ring-2 focus-visible:ring-line-selected"
        classList={{
          "transition-transform duration-200": !dragging(),
          "cursor-grab": !locked() && !dragging(),
          "cursor-grabbing": dragging(),
        }}
        style={{ width: `${HANDLE}px`, transform: `translateX(${offset()}px)`, "touch-action": "none" }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
        data-testid={props.testid ? `${props.testid}-handle` : undefined}
      >
        <Show when={props.busy} fallback={<IconChevronRight size={14} />}>
          <IconSpinner size={14} />
        </Show>
      </div>
    </div>
  )
}

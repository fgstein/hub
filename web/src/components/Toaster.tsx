import { For } from "solid-js"
import { dismiss, toasts } from "~/stores/toasts"
import { IconClose } from "./Icons"

export function Toaster() {
  return (
    <div class="pointer-events-none fixed bottom-8 right-4 z-[80] flex w-[380px] max-w-[calc(100vw-2rem)] flex-col gap-2">
      <For each={toasts}>
        {(t) => (
          <div
            role={t.kind === "error" ? "alert" : "status"}
            class="anim-in pointer-events-auto flex items-start gap-2 rounded-md bg-float px-3 py-2 text-sm shadow-float"
            classList={{
              "text-fg-critical": t.kind === "error",
              "text-fg-strong": t.kind !== "error",
            }}
            data-toast={t.kind}
          >
            <span class="min-w-0 flex-1 break-words">{t.message}</span>
            <button
              type="button"
              class="mt-0.5 text-icon hover:text-icon-hover"
              onClick={() => dismiss(t.id)}
              aria-label="Close"
            >
              <IconClose size={12} />
            </button>
          </div>
        )}
      </For>
    </div>
  )
}

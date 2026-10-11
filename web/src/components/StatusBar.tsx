import { Tooltip } from "@kobalte/core/tooltip"
import { createMemo, For, Show } from "solid-js"
import { connection, state } from "~/stores/app"

/** Discreet indicator of connection and machine reachability (page footer). */
export function StatusBar() {
  const machines = createMemo(() => state.inventory?.machines ?? [])
  const reachable = () => machines().filter((m) => m.status.reachable === true).length
  const down = () => machines().filter((m) => m.status.reachable === false)
  return (
    <footer class="flex h-7 shrink-0 items-center gap-4 border-t border-line-weak bg-bg px-3 text-xxs text-fg-weak">
      <span class="flex items-center gap-1.5" data-testid="connection" data-state={connection()}>
        <span
          class="dot"
          style={{
            "--dot-color": connection() === "open" ? "var(--status-green)" : "var(--status-orange)",
            width: "6px",
            height: "6px",
          }}
          data-blink={connection() !== "open" ? "" : undefined}
        />
        {connection() === "open"
          ? "connected"
          : connection() === "connecting"
            ? "connecting…"
            : "reconnecting…"}
      </span>
      <Show when={state.inventory}>
        <Tooltip openDelay={300}>
          <Tooltip.Trigger as="span" class="cursor-default">
            machines {reachable()}/{machines().length}
            <Show when={down().length}>
              <span class="text-fg-critical"> · {down().length} unreachable</span>
            </Show>
          </Tooltip.Trigger>
          <Tooltip.Portal>
            <Tooltip.Content class="z-[90] rounded-md bg-float px-2.5 py-1.5 text-xs text-fg shadow-float">
              <For each={machines()}>
                {(m) => (
                  <div class="flex items-center gap-2 font-mono">
                    <span
                      class="dot"
                      style={{
                        width: "6px",
                        height: "6px",
                        "--dot-color":
                          m.status.reachable === true
                            ? "var(--status-green)"
                            : m.status.reachable === false
                              ? "var(--status-red)"
                              : "var(--status-idle)",
                      }}
                    />
                    <span class="text-fg-strong">{m.id}</span>
                    <span class="truncate text-fg-weak">
                      {m.status.reachable === false
                        ? (m.status.error ?? "unreachable")
                        : m.status.latency_ms != null
                          ? `${Math.round(m.status.latency_ms)} ms`
                          : "—"}
                    </span>
                  </div>
                )}
              </For>
            </Tooltip.Content>
          </Tooltip.Portal>
        </Tooltip>
        <Show when={state.inventory?.error}>
          <span class="truncate text-fg-critical" title={state.inventory?.error ?? ""}>
            invalid inventory (previous version kept)
          </span>
        </Show>
        <span class="ml-auto font-mono">executor {state.inventory?.executor}</span>
      </Show>
    </footer>
  )
}

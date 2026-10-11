import { DropdownMenu } from "@kobalte/core/dropdown-menu"
import { createSignal, For, type JSX, Show } from "solid-js"
import { api } from "~/lib/api"
import { shellJoin } from "~/lib/command"
import type { MenuAction, RunAction, RunView } from "~/lib/types"
import { allowedActions, shellAvailable } from "~/lib/types"
import { deleteRun, displayStatus, draftFromRun, openShell, runAction } from "~/stores/app"
import { toast } from "~/stores/toasts"
import {
  IconChevronDown,
  IconCopy,
  IconDownload,
  IconPause,
  IconPlay,
  IconPower,
  IconRelaunch,
  IconRestart,
  IconStop,
  IconTerminal,
  IconTrash,
} from "./Icons"

interface ActionDef {
  label: string
  icon: (p: { size?: number }) => JSX.Element
  hint?: string
  confirm?: string
  danger?: boolean
}

const PAUSE_HINT = "docker pause gèle les processus mais ne libère pas la mémoire GPU"

export const ACTIONS: Record<MenuAction, ActionDef> = {
  pause: { label: "Pause", icon: IconPause, hint: PAUSE_HINT },
  resume: { label: "Reprendre", icon: IconPlay },
  stop: { label: "Arrêter", icon: IconStop, confirm: "Confirmer l'arrêt", danger: true },
  restart: {
    label: "Redémarrer",
    icon: IconRestart,
    hint: "Relance le même conteneur (docker start) : la commande repart du début",
  },
  relaunch: {
    label: "Relancer…",
    icon: IconRelaunch,
    hint: "Ouvre un brouillon avec les mêmes paramètres, à vérifier avant de lancer",
  },
  close: {
    label: "Fermer le conteneur",
    icon: IconPower,
    hint: "docker stop du conteneur conservé après la commande (le shell n'est plus disponible)",
  },
  delete: {
    label: "Supprimer",
    icon: IconTrash,
    confirm: "Confirmer la suppression",
    danger: true,
    hint: "docker rm : le conteneur est supprimé ; la sauvegarde des logs est conservée",
  },
}

/** Action principale selon le statut : Pause si running, Reprendre si paused, Relancer si terminé. */
export function primaryAction(status: RunView["status"]): RunAction | null {
  if (status === "running") return "pause"
  if (status === "paused") return "resume"
  if (status === "succeeded" || status === "failed" || status === "stopped") return "relaunch"
  return null
}

function perform(run: RunView, action: MenuAction) {
  if (action === "delete") return deleteRun(run.id)
  if (action === "relaunch") return draftFromRun(run.id)
  return runAction(run.id, action)
}

export function ActionButton(props: { run: RunView }) {
  const status = () => displayStatus(props.run.id)
  const primary = () => primaryAction(status())
  const [confirming, setConfirming] = createSignal<MenuAction | null>(null)
  const [open, setOpen] = createSignal(false)
  const menuActions = () => allowedActions(status(), props.run.container_alive).filter((a) => a !== primary())
  const canShell = () => shellAvailable({ status: status(), container_alive: props.run.container_alive })

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(
        props.run.full_command?.length ? shellJoin(props.run.full_command) : props.run.command,
      )
      toast("commande copiée", "success")
    } catch {
      toast("copie impossible", "error")
    }
  }

  return (
    <div
      class="flex h-7 shrink-0 items-stretch rounded-md border border-line-weak bg-bg-strong text-fg"
      onClick={(e) => e.stopPropagation()}
      data-testid="run-actions"
    >
      <button
        type="button"
        class="flex w-8 items-center justify-center rounded-l-md hover:bg-surface-hover hover:text-fg-strong disabled:opacity-30"
        disabled={!primary()}
        title={
          primary()
            ? `${ACTIONS[primary()!].label}${ACTIONS[primary()!].hint ? ` — ${ACTIONS[primary()!].hint}` : ""}`
            : "Aucune action disponible"
        }
        aria-label={primary() ? ACTIONS[primary()!].label : "Aucune action"}
        data-testid="run-primary"
        data-action={primary() ?? ""}
        onClick={() => {
          const a = primary()
          if (a) void perform(props.run, a)
        }}
      >
        <Show when={primary()} fallback={<IconPause size={13} />}>
          {(a) => ACTIONS[a()].icon({ size: 13 })}
        </Show>
      </button>
      <span class="w-px bg-line-weak" />
      <DropdownMenu
        open={open()}
        onOpenChange={(o) => {
          setOpen(o)
          if (!o) setConfirming(null)
        }}
        placement="bottom-end"
        gutter={4}
      >
        <DropdownMenu.Trigger
          class="flex w-6 items-center justify-center rounded-r-md hover:bg-surface-hover hover:text-fg-strong"
          aria-label="Autres actions"
          data-testid="run-menu"
        >
          <IconChevronDown size={12} />
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content class="menu-content z-[70] min-w-[200px] rounded-md bg-float p-0.5 shadow-float outline-none">
            <For each={menuActions()}>
              {(a) => {
                const def = ACTIONS[a]
                const needsConfirm = () => !!def.confirm && confirming() !== a
                return (
                  <DropdownMenu.Item
                    closeOnSelect={!def.confirm}
                    onSelect={() => {
                      if (needsConfirm()) {
                        setConfirming(a)
                        return
                      }
                      setConfirming(null)
                      setOpen(false)
                      void perform(props.run, a)
                    }}
                    class="flex h-7 cursor-default items-center gap-2 rounded px-2.5 text-sm outline-none data-[highlighted]:bg-surface-active"
                    classList={{
                      "text-fg-critical": def.danger && confirming() === a,
                      "text-fg-strong": !(def.danger && confirming() === a),
                    }}
                    title={def.hint}
                    data-testid={`menu-${a}`}
                  >
                    <span class="flex w-4 justify-center text-icon">{def.icon({ size: 12 })}</span>
                    {confirming() === a ? `${def.confirm} ?` : def.label}
                  </DropdownMenu.Item>
                )
              }}
            </For>
            <Show when={menuActions().length}>
              <DropdownMenu.Separator class="my-0.5 border-line-weak" />
            </Show>
            <Show when={primary() !== "relaunch"}>
              <DropdownMenu.Item
                onSelect={() => draftFromRun(props.run.id)}
                class="flex h-7 cursor-default items-center gap-2 rounded px-2.5 text-sm text-fg-strong outline-none data-[highlighted]:bg-surface-active"
                title="Ouvre un brouillon avec les mêmes paramètres (machine, image, venv, config, GPU, overrides), à modifier avant de lancer ; ce run continue"
                data-testid="menu-duplicate"
              >
                <span class="flex w-4 justify-center text-icon">
                  <IconRelaunch size={12} />
                </span>
                Nouveau run à partir de celui-ci…
              </DropdownMenu.Item>
            </Show>
            <Show when={canShell()}>
              <DropdownMenu.Item
                onSelect={() => openShell(props.run.id)}
                class="flex h-7 cursor-default items-center gap-2 rounded px-2.5 text-sm text-fg-strong outline-none data-[highlighted]:bg-surface-active"
                title="Shell interactif dans le conteneur (docker exec -it), venv activé"
                data-testid="menu-shell"
              >
                <span class="flex w-4 justify-center text-icon">
                  <IconTerminal size={12} />
                </span>
                Ouvrir un shell
              </DropdownMenu.Item>
            </Show>
            <DropdownMenu.Item
              onSelect={copyCommand}
              class="flex h-7 cursor-default items-center gap-2 rounded px-2.5 text-sm text-fg-strong outline-none data-[highlighted]:bg-surface-active"
            >
              <span class="flex w-4 justify-center text-icon">
                <IconCopy size={12} />
              </span>
              Copier la commande
            </DropdownMenu.Item>
            <DropdownMenu.Item
              as="a"
              href={api.logsUrl(props.run.id)}
              download={`trainhub-${props.run.id}.log`}
              class="flex h-7 cursor-default items-center gap-2 rounded px-2.5 text-sm text-fg-strong outline-none data-[highlighted]:bg-surface-active"
            >
              <span class="flex w-4 justify-center text-icon">
                <IconDownload size={12} />
              </span>
              Télécharger les logs
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu>
    </div>
  )
}

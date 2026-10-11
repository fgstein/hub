/** Outils pour la commande générique d'un projet. */

export const PLACEHOLDERS = ["config", "run_id", "machine", "project"] as const
const PLACEHOLDER_RE = /(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}/g

export interface Segment {
  text: string
  kind: "text" | "placeholder" | "unknown"
}

/** Découpe la commande en segments pour la coloration des placeholders. */
export function segments(command: string): Segment[] {
  const out: Segment[] = []
  let last = 0
  for (const m of command.matchAll(PLACEHOLDER_RE)) {
    const i = m.index ?? 0
    if (i > last) out.push({ text: command.slice(last, i), kind: "text" })
    const name = m[1] ?? ""
    out.push({
      text: m[0],
      kind: (PLACEHOLDERS as readonly string[]).includes(name) ? "placeholder" : "unknown",
    })
    last = i + m[0].length
  }
  if (last < command.length) out.push({ text: command.slice(last), kind: "text" })
  return out
}

/** Message d'erreur si la commande est invalide, sinon null. */
export function validateCommand(command: string): string | null {
  if (!command.trim()) return "la commande est vide"
  const names = [...command.matchAll(PLACEHOLDER_RE)].map((m) => m[1] ?? "")
  const unknown = names.filter((n) => !(PLACEHOLDERS as readonly string[]).includes(n))
  if (unknown.length) return `placeholder inconnu : ${unknown.map((u) => `{${u}}`).join(", ")}`
  if (!names.includes("config")) return "la commande doit contenir {config}"
  return null
}

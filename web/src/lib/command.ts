/** Helpers for a project's generic command. */

/** Placeholders filled in by TrainHub; any other ``{name}`` is a parameter entered at launch. */
export const PLACEHOLDERS = ["config", "config_name", "run_id", "machine", "project"] as const
const PLACEHOLDER_RE = /(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}/g

export interface Segment {
  text: string
  kind: "text" | "placeholder" | "param"
}

/** Split the command into segments to highlight placeholders. */
export function segments(command: string): Segment[] {
  const out: Segment[] = []
  let last = 0
  for (const m of command.matchAll(PLACEHOLDER_RE)) {
    const i = m.index ?? 0
    if (i > last) out.push({ text: command.slice(last, i), kind: "text" })
    const name = m[1] ?? ""
    out.push({
      text: m[0],
      kind: (PLACEHOLDERS as readonly string[]).includes(name) ? "placeholder" : "param",
    })
    last = i + m[0].length
  }
  if (last < command.length) out.push({ text: command.slice(last), kind: "text" })
  return out
}

/** Error message if the command is invalid, else null. */
export function validateCommand(command: string): string | null {
  if (!command.trim()) return "the command is empty"
  const names = [...command.matchAll(PLACEHOLDER_RE)].map((m) => m[1] ?? "")
  if (!names.includes("config")) return "the command must contain {config}"
  return null
}

/** Free parameters of the command (non-reserved placeholders), in order of appearance. */
export function commandParams(command: string): string[] {
  const names = [...command.matchAll(PLACEHOLDER_RE)].map((m) => m[1] ?? "")
  return [...new Set(names.filter((n) => !(PLACEHOLDERS as readonly string[]).includes(n)))]
}

const SAFE_WORD = /^[A-Za-z0-9_@%+=:,./{}$-]+$/

/** Escape an argument like ``shlex.quote`` (Python). */
export function shellQuote(arg: string): string {
  if (arg === "") return "''"
  if (SAFE_WORD.test(arg) && !arg.includes("${")) return arg
  return `'${arg.replaceAll("'", `'"'"'`)}'`
}

/** Join an argv into one line ready to paste into a terminal (like ``shlex.join``). */
export function shellJoin(argv: readonly string[]): string {
  return argv.map(shellQuote).join(" ")
}

/**
 * Split an argument line like a POSIX shell (single and double quotes, backslash).
 * Returns a readable error if a quote is not closed.
 */
export function splitArgs(text: string): { args: string[]; error: string | null } {
  const args: string[] = []
  let cur = ""
  let inWord = false
  let quote: "'" | '"' | null = null
  for (let i = 0; i < text.length; i++) {
    const ch = text[i] as string
    if (quote === "'") {
      if (ch === "'") quote = null
      else cur += ch
    } else if (quote === '"') {
      if (ch === '"') quote = null
      else if (ch === "\\" && i + 1 < text.length && '"\\$`'.includes(text[i + 1] as string)) {
        cur += text[++i]
      } else cur += ch
    } else if (ch === "'" || ch === '"') {
      quote = ch
      inWord = true
    } else if (ch === "\\" && i + 1 < text.length) {
      cur += text[++i]
      inWord = true
    } else if (/\s/.test(ch)) {
      if (inWord) args.push(cur)
      cur = ""
      inWord = false
    } else {
      cur += ch
      inWord = true
    }
  }
  if (quote) return { args: [], error: `unclosed ${quote} quote` }
  if (inWord) args.push(cur)
  return { args, error: null }
}

/**
 * Full command of a run, readable over several lines: the ssh part, then the remote
 * command (``docker run …``) with a single escaping level, one option per line.
 */
export function prettyCommand(argv: readonly string[]): string {
  if (!argv.length) return ""
  const lines: string[] = []
  let remote = [...argv]
  let indent = ""
  const sep = argv.indexOf("--")
  if (argv[0] === "ssh" && sep > 0 && sep === argv.length - 2) {
    lines.push(shellJoin(argv.slice(0, sep - 1)), `  ${shellJoin(argv.slice(sep - 1, sep + 1))}`)
    const parsed = splitArgs(argv[sep + 1] ?? "")
    remote = parsed.error ? [argv[sep + 1] ?? ""] : parsed.args
    indent = "    "
  }
  const n = remote.length
  const tail = n >= 4 && remote[n - 3] === "bash" && remote[n - 2] === "-lc" ? n - 4 : n
  let cur: string[] = []
  const flush = () => {
    if (cur.length) lines.push(indent + shellJoin(cur))
    cur = []
  }
  remote.slice(0, tail).forEach((tok, i) => {
    if (i > 0 && tok.startsWith("-") && i >= 2 && !["-d", "-t", "--init"].includes(tok)) flush()
    cur.push(tok)
  })
  flush()
  if (tail < n) {
    lines.push(indent + shellQuote(remote[tail] ?? ""))
    lines.push(`${indent}bash -lc ${shellQuote(remote[n - 1] ?? "")}`)
  }
  return lines.join(" \\\n")
}

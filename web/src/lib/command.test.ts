import { describe, expect, it } from "vitest"
import { prettyCommand, shellJoin, shellQuote, splitArgs } from "./command"

describe("splitArgs", () => {
  it("splits Hydra overrides while honoring quotes", () => {
    expect(splitArgs("")).toEqual({ args: [], error: null })
    expect(splitArgs("  trainer.max_epochs=5   +x=1 ").args).toEqual(["trainer.max_epochs=5", "+x=1"])
    expect(splitArgs(`model.name="a b" 'q=it"s' y=\\$HOME`).args).toEqual([
      "model.name=a b",
      'q=it"s',
      "y=$HOME",
    ])
    expect(splitArgs("a='b").error).toMatch("unclosed")
  })

  it("round-trips with shellJoin", () => {
    const argv = ["model.name=a b", "x='q'", "paths.out=${oc.env:HOME}/out", "", "z=a;b"]
    expect(splitArgs(shellJoin(argv)).args).toEqual(argv)
    expect(shellQuote("trainer.max_epochs=5")).toBe("trainer.max_epochs=5")
  })
})

describe("prettyCommand", () => {
  it("shows the remote command with a single escaping level", () => {
    const remote = shellJoin([
      "docker",
      "run",
      "-d",
      "-t",
      "--init",
      "--name",
      "th-x",
      "--gpus",
      "all",
      "-e",
      "A=b c",
      "img:1",
      "bash",
      "-lc",
      "source /v/bin/activate; python t.py 'x=a b'",
    ])
    const text = prettyCommand(["ssh", "-o", "ControlMaster=auto", "alice@h", "--", remote])
    expect(text.split(" \\\n")).toEqual([
      "ssh -o ControlMaster=auto",
      "  alice@h --",
      "    docker run -d -t --init",
      "    --name th-x",
      "    --gpus all",
      "    -e 'A=b c'",
      "    img:1",
      `    bash -lc 'source /v/bin/activate; python t.py '"'"'x=a b'"'"''`,
    ])
  })
})

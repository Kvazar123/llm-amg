// AMG memory — OpenCode plugin: the event-driven replacement for session hooks.
//
// OpenCode has no Claude-shaped hooks; its event surface is this JS plugin API. The
// plugin is a THIN adapter: every decision lives in lifecycle.py (shelled out), the
// JS only routes events and marshals data. What it wires:
//
//   session.created        -> lifecycle.py start-check  (heal + digest + the free
//                             reconcile half + the sync question) — the note is
//                             injected into the session's next user message, so the
//                             start discipline no longer depends on the model
//                             remembering to run it;
//   chat.message           -> lifecycle.py prompt-hint  (the gated "memory has gone
//                             unconsulted" reminder; silent on every gated-out
//                             prompt) — plus the flush of any pending start note;
//   session.idle           -> lifecycle.py session-end  (throttled): weights fold,
//                             digest refresh, USAGE attribution, and an INCREMENTAL
//                             transcript dump — the whole dialogue is re-fetched over
//                             the SDK and overwrites one session-stable file, so a
//                             hard kill costs at most the tail since the last idle
//                             (richer than Claude Code, where the transcript is
//                             dumped once, at session end only);
//   tool.execute.after     -> collects the files edited by the edit/write tools per
//                             session — the usage-attribution half Claude Code mines
//                             from its transcript (apply_patch edits are not
//                             attributed: its args carry a patch, not a path);
//   dispose                -> a final dump for every active session on shutdown.
//
// Subagent child sessions (parentID set) are ignored: worker dialogues are not the
// user's memory. Notes are injected as synthetic text parts — the model sees them,
// the dump renderer skips them. Installed by install.py for --env opencode; paths
// below are rendered to the configured agent directory at install time.

import { existsSync } from "node:fs"
import { join } from "node:path"

const LIFECYCLE = ".claude/skills/amg-bootstrap/scripts/lifecycle.py"
const CONFIG = ".claude/amg/config.yml"
const DUMP_THROTTLE_MS = 120_000

export const AmgMemory = async ({ client, $, directory }) => {
  // Cheap pre-gate: no AMG config in this project -> no hooks at all (matters for a
  // global install, where this plugin loads for every project the user opens).
  if (!existsSync(join(directory, CONFIG))) return {}

  const started = new Map()  // sessionID -> the start-check promise (run once)
  const pending = new Map()  // sessionID -> notes awaiting injection
  const topLevel = new Map() // sessionID -> false for subagent child sessions
  const edited = new Map()   // sessionID -> Set of files edited via edit/write
  const lastDump = new Map() // sessionID -> ts of the last transcript dump
  const timers = new Map()   // sessionID -> trailing dump timer

  // lifecycle.py prints nothing when there is nothing to say; a non-empty stdout is
  // the note/result. Failures degrade to silence — memory upkeep must never break
  // the session.
  const run = async (args, stdin) => {
    try {
      const cmd =
        stdin === undefined
          ? $`python ${LIFECYCLE} ${args} ${directory}`
          : $`python ${LIFECYCLE} ${args} ${directory} < ${new Response(stdin)}`
      const out = await cmd.cwd(directory).quiet().nothrow()
      return out.exitCode === 0 ? out.stdout.toString().trim() : ""
    } catch {
      return ""
    }
  }

  const ensureStart = (sid) => {
    let p = started.get(sid)
    if (!p) {
      p = run(["start-check"]).then((note) => {
        if (note) pending.set(sid, [...(pending.get(sid) ?? []), note])
      })
      started.set(sid, p)
    }
    return p
  }

  const isTopLevel = async (sid) => {
    if (topLevel.has(sid)) return topLevel.get(sid)
    let top = true
    try {
      const res = await client.session.get({ path: { id: sid }, query: { directory } })
      top = !res.data?.parentID
    } catch {
      /* unknown session: treat as top-level */
    }
    topLevel.set(sid, top)
    return top
  }

  const dump = async (sid) => {
    lastDump.set(sid, Date.now())
    const t = timers.get(sid)
    if (t) {
      clearTimeout(t)
      timers.delete(sid)
    }
    try {
      const res = await client.session.messages({ path: { id: sid }, query: { directory } })
      const messages = res.data ?? []
      if (!messages.length) return
      let created
      try {
        created = (await client.session.get({ path: { id: sid }, query: { directory } })).data
          ?.time?.created
      } catch {
        /* stable-name fallback inside lifecycle */
      }
      const files = [...(edited.get(sid) ?? [])]
      const out = await run(
        ["session-end"],
        JSON.stringify({
          format: "opencode",
          session_id: sid,
          created_ms: created,
          reason: "idle",
          messages,
          edited_files: files,
        }),
      )
      if (out) {
        const s = edited.get(sid)
        if (s) for (const f of files) s.delete(f)
      }
    } catch {
      /* best-effort; the next idle retries */
    }
  }

  // session.idle fires after EVERY assistant reply; the throttle keeps the dump to
  // one run per quiet window, and the trailing timer makes sure the final state
  // still lands after the last reply.
  const scheduleDump = (sid) => {
    const since = Date.now() - (lastDump.get(sid) ?? 0)
    if (since >= DUMP_THROTTLE_MS) {
      void dump(sid)
      return
    }
    if (!timers.has(sid))
      timers.set(
        sid,
        setTimeout(() => {
          timers.delete(sid)
          void dump(sid)
        }, DUMP_THROTTLE_MS - since),
      )
  }

  return {
    event: async ({ event }) => {
      const p = event.properties ?? {}
      if (event.type === "session.created" && p.info) {
        topLevel.set(p.info.id, !p.info.parentID)
        if (!p.info.parentID) void ensureStart(p.info.id)
      } else if (event.type === "session.idle" && p.sessionID) {
        if (topLevel.get(p.sessionID)) scheduleDump(p.sessionID)
      }
    },

    "tool.execute.after": async (input) => {
      if ((input.tool === "edit" || input.tool === "write") && input.args?.filePath) {
        const s = edited.get(input.sessionID) ?? new Set()
        s.add(String(input.args.filePath))
        edited.set(input.sessionID, s)
      }
    },

    // A new user message: flush the pending start note, then the gated hint — both
    // as synthetic text parts, so the model sees them with THIS prompt (the same
    // semantics as Claude Code's SessionStart/UserPromptSubmit stdout injection).
    "chat.message": async (input, output) => {
      const sid = input.sessionID
      if (!(await isTopLevel(sid))) return
      await ensureStart(sid) // covers resumed sessions, where session.created never fired
      const notes = pending.get(sid) ?? []
      pending.delete(sid)
      const text = output.parts
        .filter((p) => p.type === "text" && !p.synthetic)
        .map((p) => p.text)
        .join("\n")
      const hint = await run(["prompt-hint"], JSON.stringify({ prompt: text }))
      if (hint) notes.push(hint)
      let seq = 0
      for (const note of notes)
        output.parts.push({
          id: `prt_amg${Date.now().toString(16)}${(seq++).toString(16).padStart(4, "0")}`,
          sessionID: sid,
          messageID: output.message.id,
          type: "text",
          synthetic: true,
          text: note,
        })
    },

    dispose: async () => {
      for (const t of timers.values()) clearTimeout(t)
      timers.clear()
      for (const sid of started.keys()) if (topLevel.get(sid)) await dump(sid)
    },
  }
}

# serialwrap — Launch Video Composition Brief

> **This is the English composition prompt** (the primary deliverable). It hands a complete,
> publish-safe spec to the video generator (Hyperframes). It is grounded in the **current 0.2.2
> architecture** and **real measured pilot data** captured on a live Orange Pi over one UART.
> The Feb-2026 demo footage is **not** used (it shows a superseded tmux/marker architecture).

---

## 1. One-liner

**An AI agent drives real firmware over one serial line — two ways. serialwrap makes it faster,
cleaner, and safe for many agents at once.**

## 2. Format & tone

- **Format:** landscape, 1920×1080.
- **Duration:** ~24 s (5 scenes).
- **Tone:** polished, cinematic-leaning. Quiet, confident, systems-grade. Drama only at the open
  (the mess) and the close (the wordmark). No hype, no fake urgency.
- **Register:** honest and technical. This audience (developers, firmware/embedded engineers) rewards
  specificity and punishes spin.

## 3. Thesis (the product angle)

Not an abstract "many users share nicely" claim. A concrete contrast on **one real task**:

- **Raw tty (the baseline):** hand an agent only `/dev/ttyUSB0`. It must build its own framing — set
  the line, poke for a prompt, invent a delimiter, guess when output ends. It works, but it is slow
  and laborious.
- **serialwrap:** the agent submits a command and reads back clean, framed output. No line config, no
  marker, no guessing.

Then the payoff the bare device **cannot** match: **many agents on one wire at once, plus a human
console watching — arbitrated, serialized, zero collisions.**

## 4. What is real (source material — recreate, do not embed)

All scenes are **recreated originals**. These are the true facts to depict faithfully:

- **Board:** an Orange Pi single-board computer, reached over one UART (`ttyUSB0`, a CH340 adapter).
- **Task:** capture ~5 s of packets with `tcpdump` on `wlan0` while making one HTTPS request, then
  compute the **TCP three-way-handshake time** (SYN → SYN-ACK). Real answer landed at **~80 ms**
  (cross-checked by `curl`'s own connect timing).
- **Agent:** a headless coding agent (depict generically as "AI agent"). Both arms ran method-pure
  (every command audited); both reached a correct answer.
- **Measured difference (single pilot run per arm — illustrative, not a benchmark):**
  - **Wall-clock time:** raw tty **~8.0 min** vs serialwrap **~3.7 min**  → **~2.1× faster**.
  - **Agent effort (generated tokens):** raw tty **~2.3×** more than serialwrap.
  - Token *bill* overall was about the same — the difference is **time and effort**, not spend.
- **Raw-tty process (depict this literally — it is the visceral proof):** `stty -F /dev/ttyUSB0 …`,
  then Python opening the device, writing `\x03\r\n`, inventing a `__CHK_START__` marker, timed reads
  with retries.
- **serialwrap process:** `serialwrap cmd submit --selector COM0 --cmd '…'` → `serialwrap cmd status`
  → one clean stdout line.
- **Multi-agent (real):** two agents (`alpha`, `bravo`) with unrelated jobs fired at COM0 **at the
  same instant** (submits at +0.00/+0.08/+0.15 s). The arbiter ran them **one at a time** (execution
  windows never overlapped). All **6/6** outputs came back clean, no interleaving. A human `minicom`
  console watched the same wire live; its control was suspended during each agent command and resumed
  after.

## 5. Visual identity

- **Ground:** terminal near-black `#0d1411` (faint green bias). This is a CLI/daemon product — the
  terminal *is* its face.
- **Color = role** (from the project's own diagram language):
  - **AI agent / human** → blue `#3f8fd0` (second agent: violet `#9070d8`).
  - **serialwrap core / arbiter** → green `#46c489`.
  - **UART / WAL / IO** → amber `#e0954a`.
  - **collision / the old mess** → red `#d9553f`.
- **Type:** monospace for everything on the "wire" (commands, output, data) — the terminal voice;
  a clean humanist sans for captions and the wordmark. Large, confident, few words.
- **Restraint rule:** one accent moment per scene. Motion is crisp (0.3–0.6 s), then holds so text is
  readable (short label ~0.8 s; a sentence ~0.3 s/word).

## 6. Storyboard

### Scene 1 — The old way (hook) — 3.5 s
Dark terminal. An agent pokes a **raw serial device** by hand: `stty -F /dev/ttyUSB0 115200 raw -echo`
types in, then fragments flash — `write \x03\r\n`, `__CHK_START__`, a timed read that returns garbled,
a retry. The screen feels effortful and messy (amber device text, red glitches on the retries).
Caption **SLAMS in and holds**: **"To drive one serial port, the agent builds its own protocol."**
- Sequential/interaction: yes — the hand-rolled framing steps appear one by one, with one red retry.
- Audio: sparse keystrokes; a small dissonant tick on the retry. Music barely in, ducked.
- Transition: hard cut → Scene 2.

### Scene 2 — serialwrap (reveal) — 4.5 s
Same task. The mess resolves into two clean lines:
`serialwrap cmd submit --selector COM0 --cmd '…'` then `serialwrap cmd status` → a single framed
result line. The `serialwrap` wordmark (green) settles above it; the agent reads
**`handshake ≈ 80 ms`**. Caption holds: **"serialwrap: submit, read, done."**
- Sequential/interaction: yes — the two commands land, then the clean result; the handshake number
  counts up to 80 ms.
- Audio: one low "resolve" tick as garbage → clean; music bed steps up and stabilizes.
- Transition: soft crossfade → Scene 3.

### Scene 3 — The difference (highlight) — 4.5 s
Two lanes, same task. A time bar fills for each: **raw tty 8.0 min** (amber, long) vs
**serialwrap 3.7 min** (green, short), with **"~2× faster"** landing on the gap. A quieter secondary
line: **"and ~half the agent's effort."** Caption: **"Same wire. Same task. Half the time."**
- Sequential/interaction: yes — the two time bars grow (raw-tty bar visibly longer), then the ratio
  stamps in. Hold each readable.
- Audio: two measured beats as the bars land; one accent on "~2× faster".
- Transition: clean wipe → Scene 4.

### Scene 4 — Many agents, one wire (capability) — 7 s
The differentiator the bare device can't do. Two agent chips — **alpha (blue)** and **bravo (violet)**
— fire commands at one green **UART** lane **at the same time** (their submit tokens overlap). They
funnel through an **arbiter** node that releases them **one at a time**; six clean result rows appear
in execution order, each tagged to its own agent, none interleaved. A small **human `minicom`** panel
sits alongside, watching the same wire (it dims — "suspended" — as each agent command runs, then
brightens — "resumed"). Badges settle: **"serialized ✓  zero collision ✓  6/6"**.
Caption: **"Many agents. One wire. Nobody collides."**
- Sequential/interaction: yes — concurrent submit (overlapping) → serialized execution (one by one) →
  clean rows; the human panel suspend/resume pulses with each command.
- Audio: a steady tick per released command (rhythmic, one-at-a-time); a soft down/up pair on the
  human suspend/resume.
- Transition: dramatic-but-restrained hold → Scene 5.

### Scene 5 — Close (outro) — 4 s
Everything quiets to the **serialwrap** wordmark, green core breathing. Tagline holds:
**"One UART. Many masters. Zero collisions."** Below it: `pipx install serialwrap`.
- Audio: one low logo hit aligned to the music's strong cue (~16 s mark of the bed), then fade.
- Transition: hold → end.

**Music mood:** cinematic, restrained.
**Audio arc:** low bed enters at the open and is ducked under the mess; steps up and stabilizes when
serialwrap resolves the task; keeps a measured pulse under the time bars and the one-at-a-time agent
execution; lands one logo hit at the wordmark, then fades.

## 7. Audio direction

- **Music:** bundled `happy-beats-business-moves-vol-1` (~120 BPM), low volume throughout, ducked at
  the Scene-1 mess, faded at the end. Its strong-cue cluster (~16 s) targets the Scene-5 wordmark.
- **SFX:** sparse, motion-matched, professional — keystrokes (Scene 1), one resolve tick (Scene 2),
  one accent (Scene 3), a per-command tick + a suspend/resume pair (Scene 4), one logo hit (Scene 5).
- **Restraint:** audio never crowds the "mess → clean" contrast; no continuous busy percussion.

## 8. Publish-safe rules (hard)

- **Recreate, never embed.** No screen recordings, no participant faces, no confidential material.
- **Mask all identifiers.** Any IP → `192.168.x.x`; MACs hidden; hostname may stay generic
  (`orangepi3`). No internal paths, no internal product/CVE specifics.
- **Numbers are honest.** Show the pilot values as measured; do not inflate. The token *bill* was
  ~tied — do not imply a large token saving. Lead with **time** and **effort**, which are real.

## 9. Share copy (draft)

> Watch an AI agent drive real firmware over one serial line. Hand it a raw tty and it hand-builds a
> protocol; give it serialwrap and it just submits and reads — ~2× faster. And unlike the bare device,
> serialwrap lets many agents (and a human console) share one UART with zero collisions.
> **One UART. Many masters. Zero collisions.** 🧵

## 10. Delivery gates

- Runtime 20–25 s; every readable line holds long enough to read.
- At least one scene shows the product *doing the work* (Scenes 2 & 4 do).
- Every claim on screen is backed by the measured pilot data in §4.
- `npx hyperframes lint` / `validate` pass before render.

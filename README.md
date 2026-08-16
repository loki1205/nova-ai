# Nova

**Talk to your machine. It answers, and it acts.**

Say *"Nova, open Notepad"* and it does. Say *"what's on my screen?"* and it
looks. Speech recognition and speech synthesis both run locally; the thinking is
Claude, over the CLI you already have installed.

```
$ nova
Nova
  voice   en_GB-alba-medium
  ears    small.en · Headset (your microphone)
  wake    say 'Nova' first

  link  claude-haiku-4-5 · 97f035d1 · mcp: desktop

> Nova, open Notepad            (1.8s speech, 0.9s decode)
  [open]
< Notepad is open for you.
```

## The one thing that makes it feel alive

**It starts speaking before the answer is finished.**

Claude's replies arrive as a token stream. Nova cuts them at sentence
boundaries and speaks each one as it completes, so the first words come out
while the rest is still being written. Waiting for a turn to end costs ten to
thirty seconds of silence on anything agentic — long enough that you assume it
has crashed.

It also thinks silently: reasoning arrives on a separate channel from the
answer, so only the answer is read aloud.

## How it is wired

```
microphone → Silero VAD → Whisper → wake name → claude (persistent session) → Piper
                                                        ↓
                                                 desktop-control
```

One long-lived `claude` process, spoken to over `--input-format stream-json`.
That gives three things a per-question `claude -p` cannot: streaming replies,
memory across turns, and no repeated startup cost.

If [desktop-control](../desktop-control) is installed, Nova picks it up
automatically and can see the screen and click things. Without it, Nova still
talks — it just has no hands.

## Requirements

- Windows 10 or 11
- Python 3.10+
- [Claude Code](https://claude.com/claude-code) on PATH
- A microphone

## Install

```powershell
.\setup.ps1
```

Builds the venv, installs dependencies, downloads the speech models (~560 MB,
once), and reports whether desktop-control is connected.

## Use it

```
nova                    listen
nova --text             type instead of talking
nova --say "..."        one question, then exit
nova --mute             no spoken replies
nova --mic-test         is the microphone delivering audio?
nova --devices          list input devices
```

Say the wake name — *"Nova, what's in front of me?"* It can appear **anywhere**
in the sentence, not just at the front, so "so I was thinking Nova, let's try
something else" works. Once it has answered, follow-ups need no name for 60
seconds.

## The wake name

An always-on microphone is only tolerable if it ignores most of what it hears.
Without a wake name, arming the mic means every word in the room is transcribed
and sent, including the half of a phone call the assistant should never have
heard.

Matching is deliberately forgiving — in one session Whisper rendered "Jarvis"
as *Jollis*, *JAWS* and *JAVIS* — because being ignored is the expensive
failure, not being woken by a near miss.

But forgiveness cuts both ways on short names. "Nova" is one edit from "now"
and two from "no", so a plain fuzzy match woke it on *"now close the window"*
and *"no I don't think that works"*. Very common words therefore need an
**exact** match; everything else is matched loosely. Pick a distinctive name and
this problem mostly goes away.

Turn it off with `"wake": {"required": false}` — but read the paragraph above
first.

## Settings

`config.example.json` is the tracked template; your own settings live in
`config.json`, which is gitignored. `setup.ps1` copies one to the other on first
run, and Nova does the same if you skip setup. Re-running setup never overwrites
it, so your microphone and voice survive an update — but a key added to the
example later will not appear in a `config.json` you already have. Diff the two
after pulling if something new is documented and missing.

| Key | What it does |
|---|---|
| `claude.model` | Which model thinks. Haiku is noticeably snappier for conversation |
| `claude.allowed_tools` | Exactly which tools it may use without asking. Desktop tools are granted; Bash, Write and Edit are not |
| `claude.system_prompt` | How it speaks and how it acts |
| `wake.name` / `wake.required` | What to call it, and whether you must |
| `ears.model` | `small.en` for accuracy, `base.en` for speed |
| `ears.input_device` | Microphone, matched by name fragment. `null` uses the system default; `nova --devices` lists what is plugged in |
| `ears.silence_ms` | How long a pause means you have finished |
| `ears.barge_in` | Keep listening while it speaks, so you can cut it off. **Off by default** — only turn it on with headphones, or it hears itself and answers itself forever |
| `tts.piper_voice` | Any voice from rhasspy.github.io/piper-samples |

## What it will not do without asking

Tool permissions are granted by name, not switched off. The desktop tools are
allowed; `Bash`, `Write` and `Edit` still need a human, so a misheard sentence
cannot reach a shell.

Within the desktop tools, anything irreversible — sending, deleting, paying,
submitting — is refused until confirmed. Nova reads the action back and waits
for a clear yes. That guard exists because the instruction arrived by
microphone: this project's own dictation once turned "cancel" into "Kensin".

## Known limits

- **Windows only**, and English works best.
- **Latency is ~3-8 seconds** to the first spoken word, most of it model time.
- **Barge-in assumes headphones.** With `ears.barge_in` on, the microphone
  stays live while it speaks, so you can cut it off mid-sentence. On laptop
  speakers turn it off, or it hears its own voice, answers itself, and never
  stops. That is what half duplex is protecting you from.
- Whisper on a CPU decodes 5-10s of speech in 1-3s. There is no GPU path here.

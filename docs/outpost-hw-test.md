# Hardware testing with Devin Outposts

Cloud Devin sessions cannot reach the Reachy Mini on your desk. A
[Devin Outpost](https://docs.devin.ai/cloud/outposts/overview) lets a Devin
session execute its commands on the Mac that Reachy is plugged into, so a
session can check out a PR, run it against the real robot and report back,
replacing the manual "pull, test locally, merge" loop. The worker only opens
an **outbound** HTTPS connection to Devin; no inbound ports, VPN or public IP
are needed.

`uv run ambient-hw-test --pr <n>` is the deterministic part of that loop. The
playbook below tells the Outpost session to run it and report the result.

## What `ambient-hw-test` does

1. `checkout` — `git fetch origin pull/<n>/head` (or `--ref`) and check it out
   detached into `~/.ambient-home/hw-test/worktree`. The live checkout that
   the LaunchAgents run from is never modified.
2. `uv sync`, then `gate` — `ruff check`, `ruff format --check`, `mypy`,
   `pytest` (read-only; nothing is auto-fixed).
3. Pause `ai.ambient-home.app` with `launchctl bootout` so two copies don't
   compete for the microphone and UI port. It is bootstrapped again when the
   run ends, including on failure.
4. `daemon` — wait for `GET /api/daemon/status` to report `running`. The
   daemon LaunchAgent is reused as-is.
5. `mic` — `ambient-mic-check` (XMOS silent-mic probe).
6. `motion` — move only the antennas to ±0.3 rad, confirm the reported joint
   positions, then return them to where they started. No head or body motion.
7. `app` — start `ambient-home` from the worktree on UI port 8766 with a fresh
   data directory (`~/.ambient-home/hw-test/data`, so spend and job history
   are untouched), wait for `/api/status` to report `asleep`, and check the
   log for `Starting ambient-home` and `Reachy daemon ready`. With
   `--wake-command "say 'hey jarvis'"` it also speaks the wake word through the
   Mac speaker, expects a Live session to open, and stops it via `/api/stop`.
   That opens a real, paid OpenAI Live session.

Output: a Markdown table on stdout (paste-ready for a PR comment),
`~/.ambient-home/hw-test/report.json`, and `~/.ambient-home/hw-test/app.log`.
Exit code 0 means every non-skipped check passed.

`DEVIN_API_KEY`/`DEVIN_ORG_ID` are removed from the app's environment unless
`--allow-dispatch` is passed, so a test run cannot dispatch paid Devin jobs.
App secrets come from `--env-file` (default `~/.config/ambient-home/hw-test.env`),
not from the repo `.env`.

Without hardware (CI-like check, or on a cloud VM):

```bash
uv run reachy-mini-daemon --mockup-sim --headless --no-media &
uv run ambient-hw-test --ref main --skip-mic
```

## One-time setup on the Mac

Do this on a Mac dedicated to the robot (e.g. a Mac mini). At minimum, use a
separate **standard (non-admin) macOS user**, never your main account:
sessions run with the worker user's permissions, so its keychain, `~/.ssh`
and browser profile are exposed to the agent.

1. Create the user (e.g. `devin-runner`), log in as it, install `uv`, `git`
   and `ffmpeg` (for recordings), and do **not** give it passwordless sudo.
2. Clone the repo into the worker's directory with a **read-only** credential
   (a GitHub deploy key without write access, or a fine-grained token with
   Contents: read):

   ```bash
   mkdir -p ~/devin-worker/repos && cd ~/devin-worker/repos
   git clone git@github.com:sesh11/ambient-home.git
   cd ambient-home && uv sync && uv run ambient-install-service
   ```

3. Create `~/.config/ambient-home/hw-test.env` (mode `600`) with a test-only
   `OPENAI_API_KEY` from a separate OpenAI project with a hard monthly budget.
   Leave out `DEVIN_API_KEY`.
4. Install the [Devin CLI](https://docs.devin.ai/cli), create a **macOS**
   outpost (Settings → Environment → Outposts, e.g. `reachy`), save its token,
   and start the worker from `~/devin-worker`:

   ```bash
   cd ~/devin-worker
   devin worker start --outpost=reachy --token=<outpost-token>
   ```

   One worker means one session at a time, so sessions queue for the single
   robot instead of fighting over it.
5. In Devin, bind a [security profile](https://docs.devin.ai/product-guides/security-profiles)
   to these sessions with **Git access: Read-only**, **Devin MCP: read-only**
   and the GitHub CLI token removed. The tester can then report but cannot push,
   merge or spawn other sessions.
6. Devin does **not** enforce network allowlists on outposts. If you want an
   egress allowlist, enforce it on the Mac (LuLu or `pf`) for the worker user:
   Devin API/gateway, GitHub, PyPI/astral (uv), Hugging Face (Pollen assets)
   and `api.openai.com`.

Kill switch: stop the worker process, or revoke the outpost token in
Settings → Environment → Outposts.

Physical safety: the agent can move the robot and use its microphone and
camera. Keep Reachy somewhere clear and away from private spaces while the
worker is running.

## Running a test

From Slack:

```
@Devin !outpost reachy run the ambient-home hardware test playbook for PR #<n>
```

## Playbook

Save as a Devin playbook (e.g. "ambient-home hardware test"):

> You are running on the `reachy` outpost: a Mac with a Reachy Mini Lite
> attached over USB. Do not edit, commit or push code, and do not touch the
> live checkout's working tree other than `git fetch`.
>
> 1. `cd ~/devin-worker/repos/ambient-home && git fetch origin`.
> 2. Run `uv run ambient-hw-test --pr <n>` (add `--wake-command "say 'hey jarvis'"`
>    only if the user asked for a wake test). Do not pass `--allow-dispatch`.
> 3. If a check fails, read `~/.ambient-home/hw-test/app.log`,
>    `~/Library/Logs/ambient-home/daemon.log` and the failing command output,
>    and say whether it looks like a code bug or an environment/hardware issue
>    (unplugged robot, silent mic, daemon down).
> 4. If the user asked for feature-specific checks beyond the harness, run them
>    from `~/.ambient-home/hw-test/worktree` while the app agent is paused, and
>    restore it afterwards with
>    `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.ambient-home.app.plist`.
> 5. Reply with the Markdown table the harness printed, the commit SHA tested,
>    and a one-line verdict. Confirm that `launchctl print gui/$UID/ai.ambient-home.app`
>    shows the agent running again.

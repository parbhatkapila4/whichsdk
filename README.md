# whichsdk

Runs a coding agent on a real task in a clean container and saves what it installs.

## Why it exists

When a developer asks a coding agent to add auth or payments to their app, the agent picks the SDK, installs it and writes the code. That pick is now a distribution channel for every devtool company, and nobody measures it. whichsdk runs real tasks through coding agents in clean containers and saves what they actually install: the full transcript, plus the `package.json` and lockfile each run leaves behind.

## Status

Early. Claude Code is the only agent so far, and there is one study: auth for Next.js. The CLI has two commands, `init` and `run-one`, so you run one task at a time. Detection and reporting are not built yet, and nothing reads the saved runs.

## How it works

1. `run-one` reads the study file. It adds up what the study has already spent and refuses to start if one more run could take it over the study budget.
2. It starts a fresh container for the attempt, copies the starter project in with `docker cp` and commits it to git as a baseline.
3. It copies the baseline `package.json` and lockfile out to `runs/<run id>/baseline/`.
4. It runs Claude Code headless (`claude -p`) in the container with the task as the prompt. The model, turn limit and per-run spend cap come from the study file, and the image pins the Claude Code version.
5. It saves the raw output to `transcript.jsonl`, copies `package.json` and any lockfile out again next to the baseline copies, and deletes the container.
6. It records the attempt in SQLite: status, exit code, cost, duration, model, Claude Code version and the transcript path.

## Quickstart

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), Docker with Linux containers and an Anthropic API key.

Run every command from the repo root. `.env`, the starter path in the study file, `runs/` and `whichsdk.sqlite` are all relative to the current directory.

Build the sandbox image first. Runs use `--pull never`, so a run fails if the image is missing.

```sh
docker build -f docker/Dockerfile -t whichsdk-sandbox templates/nextjs-starter
```

Put your key in a file named `.env` in the repo root. The file is already in `.gitignore`.

```
ANTHROPIC_API_KEY=your-key-here
```

The key is passed into the container for Claude Code to use, and the agent runs with permission prompts off, so use a key you can revoke.

Run one task. This calls the API and costs money. The shipped study sets a 1.00 USD cap per run.

```sh
uv run whichsdk run-one studies/auth-nextjs.yaml --task 0
```

`uv run` installs the Python dependencies on first use. The command prints what the study has spent before it starts, and a summary when it finishes. Output lands in the directory you ran from. The run id joins the study, task, agent and repeat:

```
runs/auth-nextjs-t0-claude_code-r0/
  transcript.jsonl     Claude Code's raw output: stream-json plus stderr
  package.json         the project files after the agent ran
  package-lock.json    and any other lockfile
  baseline/            the same files before the agent started
  attempts/1/          the files of attempt 1, after a retry
whichsdk.sqlite        studies, runs and events
```

Comparing `package.json` with `baseline/package.json` shows what the agent added.

To run the same task again, pass another `--repeat` (0 to 2 in the shipped study). A run that is already done is refused. After a failure, running the same command again starts a new attempt.

## A study file

This is `studies/auth-nextjs.yaml` cut down to two competitors and one task. Every field is required and unknown fields are rejected. `uv run whichsdk init my-study` writes `studies/my-study.yaml` with a placeholder competitor and task to edit.

```yaml
name: auth-nextjs
category: auth                 # label; nothing reads it yet
competitors:                   # the SDKs being compared; nothing reads them yet
  - name: Clerk
    npm_packages:
      - "@clerk/nextjs"
  - name: Better Auth
    npm_packages:
      - better-auth
starter_template: templates/nextjs-starter   # copied into every container
tasks:                         # sent to the agent as written; --task picks one
  - >-
    Add authentication to this app. People should be able to sign up with an email and
    password, log in and log out, and I want a /dashboard page that only logged-in users can see.
agents:                        # run-one uses the first entry
  - name: claude_code          # the only agent so far
    model: claude-sonnet-5-5   # passed as --model
repeats: 3                     # --repeat takes 0 to repeats - 1
timeout_seconds: 1800          # per attempt, enforced inside the container
max_turns: 60                  # passed as --max-turns
run_budget_usd: 1.00           # passed as --max-budget-usd
study_budget_usd: 10.00        # cap on the total of every attempt
```

The shipped file has two more competitors and two more tasks. Three tasks times three repeats is nine runs, each with a 1.00 USD cap, against a 10.00 USD study cap.

## Why it works this way

**Retries never delete earlier attempts.** Each attempt is its own row in SQLite, and on a retry the old files move from `runs/<run id>/` to `runs/<run id>/attempts/<n>/`. A failed attempt still cost money and still shows what the agent did, so it stays in the record.

**There are two spend caps.** `run_budget_usd` goes to Claude Code as `--max-budget-usd`. `study_budget_usd` is a check whichsdk makes itself before each run: it adds up every attempt, failed ones included, and refuses to start if that plus the run cap would go over. An attempt that started the agent but has no reported cost, for example one cut off before Claude Code wrote its final result event, counts at the full run budget. Counting it as zero would let a string of timeouts slip past the study cap. The database keeps that cost empty.

**Runs record the model and the Claude Code version.** Which SDK an agent picks can change from one model or agent version to the next. The image pins Claude Code and turns off its auto-updater, so the version only changes when you rebuild the image. A run that fails before the agent starts has the model but no version.

**The starter goes in with `docker cp`, not a bind mount.** Bind mounts from a Windows drive are slow. A copy also leaves the host files out of the agent's reach.

**The API key never goes on a command line.** It reaches docker through the environment (`-e ANTHROPIC_API_KEY`, no value). Command lines show up in process listings, and the error for a failed docker call quotes its arguments. The transcript is the file you keep and might share, so every occurrence of the key in the output is replaced with `[redacted]` before it is written.

**The starter has no auth, database or UI libraries.** Its dependencies are `next`, `react` and `react-dom`, plus TypeScript and its type packages. A library already in `package.json` would steer the agent toward it. The study prompts follow the same rule: none names a competitor, and a test checks it.

## What's next

- A Codex adapter, so a second agent can run the same study.
- Detectors that read a saved run and report what was installed, what was imported, whether the agent switched SDKs partway, and whether the project builds.
- An HTML report.
- The first full study.

## License

MIT. See [LICENSE](LICENSE).

# whichsdk

Runs a coding agent on a real task in a clean container and saves what it installs.

## Why it exists

When a developer asks a coding agent to add auth or payments to their app, the agent picks the SDK, installs it and writes the code. That pick is now a distribution channel for every devtool company, and nobody measures it. whichsdk runs real tasks through coding agents in clean containers and saves what they actually install: the full transcript, the `package.json` and lockfile each run leaves behind, the code it changed and a build log. Detectors then decide which SDK each run picked.

## Status

Early. Claude Code is the only agent so far, and there is one study: auth for Next.js. The CLI has five commands: `init`, `run-one`, `run`, `status` and `detect`. Each finished run gets a verdict and `status` counts them, but there is no report yet.

## How it works

1. `run-one` reads the study file. It adds up what the study has already spent and refuses to start if one more run could take it over the study budget.
2. It starts a fresh container for the attempt, copies the starter project in with `docker cp` and commits it to git as a baseline.
3. It copies the baseline `package.json` and lockfile out to `runs/<run id>/baseline/`.
4. It runs Claude Code headless (`claude -p`) in the container with the task as the prompt. The model, turn limit and per-run spend cap come from the study file, and the image pins the Claude Code version.
5. It saves the raw output to `transcript.jsonl`, copies `package.json` and any lockfile out again next to the baseline copies, saves the code changes against the baseline commit as `changes.diff`, runs `npm run build` and keeps the last 200 lines of its output as `build.log`, then deletes the container.
6. It runs the detectors on the saved files and records the attempt in SQLite: status, exit code, cost, duration, model, Claude Code version, the verdict with the packages behind it, and the build result.

`run` repeats these steps for every run of the study that is not done yet, as described under Quickstart.

The detectors compare the package files with the baseline, scan `changes.diff` for imports of each competitor's packages and for added code, and read the transcript for install commands, switches between competitors, and web searches and fetches. A package counts as installed when `package.json` lists it and, if there is a lockfile, the lockfile has it. The verdict is one of six:

- `picked`: one competitor's package was added and the new code imports it.
- `installed_unused`: one competitor's package was added and nothing imports it.
- `multiple`: more than one competitor's packages were left installed.
- `other`: packages were added and none belongs to a competitor. They are saved with the result, in its `added` column.
- `no_sdk`: `package.json` and the lockfiles are byte for byte the same as the baseline, the transcript has no command that installs a package by name, no competitor is imported, and the diff adds JavaScript or TypeScript code. The agent built it itself.
- `undetermined`: anything the evidence does not show. It is always saved with a `reason`.

A run whose status is not `done` is always `undetermined`, whatever it left behind, because it shows only part of what the agent would have done. Its reason is `max_turns` when the result event in the transcript says the turn limit stopped it, and `run_failed` otherwise. The other reasons are `package_json_unreadable`, `competitor_import_without_install`, `competitor_declared_without_lock`, `no_code_added`, `lockfile_changed_without_package` (a package file changed but no package was added) and `install_command_changed_nothing`. The first one that applies goes in the `reason` column of `results`.

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

`uv run` installs the Python dependencies on first use. The command prints what the study has spent before it starts, and a summary with the verdict when it finishes. Output lands in the directory you ran from. The run id joins the study, task, agent and repeat:

```
runs/auth-nextjs-v2-t0-claude_code-r0/
  transcript.jsonl     Claude Code's raw output: stream-json plus stderr
  package.json         the project files after the agent ran
  package-lock.json    and any other lockfile
  baseline/            the same files before the agent started
  changes.diff         what the agent changed against the baseline, lockfiles left out
  build.log            the last 200 lines of npm run build
  attempts/1/          the files of attempt 1, after a retry
whichsdk.sqlite        studies, runs, events and results
```

To run the same task again with `run-one`, pass another `--repeat` (0 to 4 in the shipped study). A run that is already done is refused. After a failure, running the same command again starts a new attempt.

Run the whole study with `run`. It starts every task, repeat and agent that is not done yet, repeat by repeat, so a study that runs out of budget has covered every task evenly. Failed runs are skipped unless you pass `--retry-failed`, and so is a run left in the running state by a crash. This calls the API and costs money.

```sh
uv run whichsdk run studies/auth-nextjs.yaml --concurrency 3
```

It prints one line per finished attempt: task, repeat, verdict with the competitor or packages, cost, and the cost reported so far in the study. Before each start it applies the budget check with every run still going counted at the full run budget, so parallel runs cannot take the study past its cap. Ctrl-C marks the runs in flight as failed, removes their containers and exits with code 130. Run the command again to carry on with the rest; add `--retry-failed` to run the interrupted ones again. Use one `run` at a time per database.

`status` shows how many runs are done, failed, running and pending, the spend as the budget check counts it, and how many runs have each verdict (the newest attempt of each run). It reads only the database.

```sh
uv run whichsdk status studies/auth-nextjs.yaml
```

To run the detectors again on the saved runs of a study, for example after fixing a package list, use `detect`. It calls no agent and starts no container, and it keeps the saved build results because the containers they ran in are gone. Results saved before the `reason` column existed get their reason this way.

```sh
uv run whichsdk detect studies/auth-nextjs.yaml
```

## A study file

This is `studies/auth-nextjs.yaml` cut down to two competitors and one task. Every field is required and unknown fields are rejected. `uv run whichsdk init my-study` writes `studies/my-study.yaml` with a placeholder competitor and task to edit.

```yaml
name: auth-nextjs-v2           # the start of every run id
category: auth                 # label; nothing reads it yet
competitors:                   # the SDKs being compared, by npm package
  - name: Clerk
    npm_packages:
      - "@clerk/nextjs"
  - name: Better Auth
    npm_packages:
      - better-auth
starter_template: templates/nextjs-starter   # copied into every container
tasks:                         # sent to the agent as written; --task picks one
  - >-
    add login to this app
agents:                        # run-one uses the first entry, run uses all of them
  - name: claude_code          # the only agent so far; names must be distinct
    model: claude-sonnet-5-5   # passed as --model
repeats: 5                     # --repeat takes 0 to repeats - 1
timeout_seconds: 1800          # per attempt, enforced inside the container
max_turns: 60                  # passed as --max-turns
run_budget_usd: 1.00           # passed as --max-budget-usd
study_budget_usd: 15.00        # cap on the total of every attempt
```

The shipped file has six more competitors and nine more tasks, with ten npm packages in all. Ten tasks times five repeats is 50 runs. At the full 1.00 USD cap each they would cost 50.00 USD, so the 15.00 USD study cap is what limits the study: `run-one` and `run` refuse to start a run once the spend so far plus 1.00 USD would go over 15.00 USD. List every npm package a competitor ships, because the detectors count only the packages named here.

## Why it works this way

**Retries never delete earlier attempts.** Each attempt is its own row in SQLite, and on a retry the old files move from `runs/<run id>/` to `runs/<run id>/attempts/<n>/`. A failed attempt still cost money and still shows what the agent did, so it stays in the record.

**There are two spend caps.** `run_budget_usd` goes to Claude Code as `--max-budget-usd`. `study_budget_usd` is a check whichsdk makes itself before each run: it adds up every attempt, failed ones included, and refuses to start if that plus the run cap would go over. An attempt that is still running, or that started the agent and has no reported cost, for example one cut off before Claude Code wrote its final result event, counts at the full run budget. Counting it as zero would let parallel runs, or a string of timeouts, slip past the study cap. A run you stop with Ctrl-C usually has no result event either, so it stays counted at the full budget. The database keeps that cost empty.

**Runs record the model and the Claude Code version.** Which SDK an agent picks can change from one model or agent version to the next. The image pins Claude Code and turns off its auto-updater, so the version only changes when you rebuild the image. A run that fails before the agent starts has the model but no version.

**The starter goes in with `docker cp`, not a bind mount.** Bind mounts from a Windows drive are slow. A copy also leaves the host files out of the agent's reach.

**The API key never goes on a command line.** It reaches docker through the environment (`-e ANTHROPIC_API_KEY`, no value). Command lines show up in process listings, and the error for a failed docker call quotes its arguments. The transcript is the file you keep and might share, so every occurrence of the key in the output is replaced with `[redacted]` before it is written.

**The starter has no auth, database or UI libraries.** Its dependencies are `next`, `react` and `react-dom`, plus TypeScript and its type packages. A library already in `package.json` would steer the agent toward it. The study prompts follow the same rule: none names a competitor, and a test checks it.

## What's next

- A Codex adapter, so a second agent can run the same study.
- An HTML report.
- The first full study.

## License

MIT. See [LICENSE](LICENSE).

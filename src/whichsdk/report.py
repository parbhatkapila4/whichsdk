import html
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from typing import get_args

from whichsdk.detect.verdict import Verdict
from whichsdk.spec import StudySpec
from whichsdk.store import Outcome, Run, Spend

Z_95 = 1.96
AXIS_STEP = 0.1
PLOT_WIDTH = 480
PLOT_HEIGHT = 28
BAR_HEIGHT = 16
WHISKER_CAP = 6


@dataclass(frozen=True)
class Rate:
    """Some runs out of all runs, as a share with its 95% Wilson range."""

    runs: int
    rate: float
    low: float
    high: float


@dataclass(frozen=True)
class CompetitorRow:
    """How often a competitor was picked."""

    name: str
    picks: Rate


@dataclass(frozen=True)
class TaskRow:
    """What the runs of one task got: the count of each outcome and the outcome of each repeat."""

    index: int
    prompt: str
    runs: int
    outcomes: dict[str, int]
    repeats: list[str]


@dataclass(frozen=True)
class BuildRow:
    """How many of a verdict's runs that have a build result passed it."""

    verdict: str
    runs: int
    built: int
    passed: int
    pass_rate: float | None


@dataclass(frozen=True)
class SwitchRow:
    """A competitor an agent installed and then replaced with another."""

    run_id: str
    task: int
    repeat: int
    from_competitor: str
    to_competitor: str


@dataclass(frozen=True)
class Methods:
    """How the study was run."""

    agents: list[str]
    models: list[str]
    claude_code_versions: list[str]
    starter: str
    tasks: int
    repeats: int
    expected_runs: int
    helper_packages: list[str]
    attempts: int
    total_cost_usd: float
    unreported_attempts: int
    first_run: str
    last_run: str
    generated: str


@dataclass(frozen=True)
class Report:
    """Every number of a study report."""

    study: str
    category: str
    total_runs: int
    no_sdk: Rate
    competitors: list[CompetitorRow]
    verdicts: dict[str, int]
    tasks: list[TaskRow]
    builds: list[BuildRow]
    switches: list[SwitchRow]
    methods: Methods


def build_report(
    spec: StudySpec, outcomes: list[Outcome], attempts: list[Run], spend: Spend, generated: date
) -> Report:
    """Work out a study's report from the newest attempt of each run, which cannot be empty."""
    total = len(outcomes)
    verdicts = {
        verdict: sum(outcome.detection.verdict == verdict for outcome in outcomes)
        for verdict in get_args(Verdict)
    }
    picks = Counter(
        outcome.detection.picked for outcome in outcomes if outcome.detection.verdict == "picked"
    )
    competitors = sorted(
        (CompetitorRow(c.name, _rate(picks[c.name], total)) for c in spec.competitors),
        key=lambda row: -row.picks.runs,
    )
    return Report(
        study=spec.name,
        category=spec.category,
        total_runs=total,
        no_sdk=_rate(verdicts["no_sdk"], total),
        competitors=competitors,
        verdicts=verdicts,
        tasks=[_task_row(spec, index, outcomes) for index in range(len(spec.tasks))],
        builds=[_build_row(verdict, outcomes) for verdict in get_args(Verdict)],
        switches=[
            SwitchRow(
                outcome.run.id,
                outcome.run.task_index,
                outcome.run.repeat,
                switch.from_competitor,
                switch.to_competitor,
            )
            for outcome in outcomes
            for switch in outcome.detection.switches
        ],
        methods=_methods(spec, outcomes, attempts, spend, generated),
    )


def report_json(report: Report) -> str:
    """Return the report's numbers as indented JSON."""
    return json.dumps(asdict(report), indent=2) + "\n"


def render_html(report: Report) -> str:
    """Return the report as one HTML page with its style and charts inline and no script."""
    methods = report.methods
    agents = _e(", ".join(methods.agents))
    summary = f"{_e(report.category)}. {report.total_runs} runs of {agents}"
    parts = [
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(report.study)}: which SDK does the agent pick?</title>",
        f"<style>\n{STYLE}</style>\n</head>\n<body>\n<main>\n<header>",
        '<p class="eyebrow">whichsdk study report</p>',
        f"<h1>{_e(report.study)}</h1>",
        f'<p class="sub">{summary}, {methods.first_run} to {methods.last_run}.</p>\n</header>',
        _headline(report),
        _tasks(report),
        _builds(report),
        _switches(report),
        _methods_box(report),
        "</main>\n</body>\n</html>\n",
    ]
    return "\n".join(parts)


def _wilson(successes: int, total: int) -> tuple[float, float]:
    p = successes / total
    spread = 1 + Z_95**2 / total
    centre = (p + Z_95**2 / (2 * total)) / spread
    half = Z_95 * math.sqrt(p * (1 - p) / total + Z_95**2 / (4 * total**2)) / spread
    return max(0.0, centre - half), min(1.0, centre + half)


def _rate(runs: int, total: int) -> Rate:
    low, high = _wilson(runs, total)
    return Rate(runs, round(runs / total, 4), round(low, 4), round(high, 4))


def _label(outcome: Outcome) -> str:
    detection = outcome.detection
    return detection.picked if detection.picked is not None else detection.verdict


def _task_row(spec: StudySpec, index: int, outcomes: list[Outcome]) -> TaskRow:
    labels = [_label(outcome) for outcome in outcomes if outcome.run.task_index == index]
    counts = Counter(labels)
    return TaskRow(
        index=index,
        prompt=" ".join(spec.tasks[index].split()),
        runs=len(labels),
        outcomes=dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        repeats=labels,
    )


def _build_row(verdict: str, outcomes: list[Outcome]) -> BuildRow:
    runs = [outcome for outcome in outcomes if outcome.detection.verdict == verdict]
    built = [outcome for outcome in runs if outcome.build_passed is not None]
    passed = sum(bool(outcome.build_passed) for outcome in built)
    rate = round(passed / len(built), 4) if built else None
    return BuildRow(verdict, len(runs), len(built), passed, rate)


def _methods(
    spec: StudySpec, outcomes: list[Outcome], attempts: list[Run], spend: Spend, generated: date
) -> Methods:
    runs = [outcome.run for outcome in outcomes]
    versions = {run.claude_code_version for run in runs if run.claude_code_version}
    return Methods(
        agents=[agent.name for agent in spec.agents],
        models=sorted({run.model for run in runs if run.model}),
        claude_code_versions=sorted(versions),
        starter=spec.starter_template.as_posix(),
        tasks=len(spec.tasks),
        repeats=spec.repeats,
        expected_runs=len(spec.tasks) * spec.repeats * len(spec.agents),
        helper_packages=spec.helper_packages,
        attempts=len(attempts),
        total_cost_usd=round(spend.reported_usd, 2),
        unreported_attempts=spend.unreported_attempts,
        first_run=min(run.started_at[:10] for run in runs if run.started_at),
        last_run=max((run.finished_at or run.started_at or "")[:10] for run in runs),
        generated=generated.isoformat(),
    )


def _e(text: str) -> str:
    return html.escape(text)


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _range_text(rate: Rate) -> str:
    return f"{_pct(rate.low)} to {_pct(rate.high)}"


def _cell(tag: str, text: str, scope: str = "", numeric: bool = False) -> str:
    attributes = (f' scope="{scope}"' if scope else "") + (' class="num"' if numeric else "")
    return f"<{tag}{attributes}>{text}</{tag}>"


def _table(caption: str, headers: list[str], rows: list[list[str]], numeric: set[int]) -> str:
    head = "".join(_cell("th", name, "col", i in numeric) for i, name in enumerate(headers))
    body = "".join(
        "<tr>"
        + _cell("th", row[0], "row")
        + "".join(_cell("td", text, numeric=i in numeric) for i, text in enumerate(row) if i)
        + "</tr>\n"
        for row in rows
    )
    return (
        f'<div class="scroll"><table>\n<caption>{_e(caption)}</caption>\n'
        f"<thead><tr>{head}</tr></thead>\n<tbody>\n{body}</tbody>\n</table></div>"
    )


def _axis_max(report: Report) -> float:
    highest = max([report.no_sdk.high, *(row.picks.high for row in report.competitors)])
    return min(1.0, math.ceil(highest / AXIS_STEP) * AXIS_STEP)


def _line(css: str, x1: float, x2: float, y1: float, y2: float) -> str:
    return f'<line class="{css}" x1="{x1}" x2="{x2}" y1="{y1}" y2="{y2}"/>'


def _plot(rate: Rate, axis_max: float, hatched: bool) -> str:
    def x(value: float) -> float:
        return round(value / axis_max * PLOT_WIDTH, 1)

    middle = PLOT_HEIGHT / 2
    grid = "".join(
        _line("grid", x(step * AXIS_STEP), x(step * AXIS_STEP), 0, PLOT_HEIGHT)
        for step in range(round(axis_max / AXIS_STEP) + 1)
    )
    whiskers = (
        _line("whisker", x(rate.low), x(rate.high), middle, middle)
        + _line("whisker", x(rate.low), x(rate.low), middle - WHISKER_CAP, middle + WHISKER_CAP)
        + _line("whisker", x(rate.high), x(rate.high), middle - WHISKER_CAP, middle + WHISKER_CAP)
    )
    bar = (
        f'<rect x="0" y="{(PLOT_HEIGHT - BAR_HEIGHT) / 2}" width="{x(rate.rate)}" '
        f'height="{BAR_HEIGHT}" fill="{"url(#hatch)" if hatched else "currentColor"}"/>'
    )
    return (
        f'<svg class="plot" viewBox="0 0 {PLOT_WIDTH} {PLOT_HEIGHT}" aria-hidden="true" '
        f'focusable="false">{HATCH if hatched else ""}{grid}{bar}{whiskers}</svg>'
    )


def _chart(report: Report) -> str:
    axis_max = _axis_max(report)
    ticks = "".join(
        f'<span style="left:{step * AXIS_STEP / axis_max * 100:.1f}%">{step * 10}%</span>'
        for step in range(round(axis_max / AXIS_STEP) + 1)
    )
    entries = [("No SDK", "no_sdk", report.no_sdk, True)] + [
        (row.name, "picked", row.picks, False) for row in report.competitors
    ]
    rows = "".join(
        f'<li class="row{" hand" if hatched else ""}">'
        f'<span class="name">{_e(name)}<small>{kind}</small></span>'
        f"{_plot(rate, axis_max, hatched)}"
        f'<span class="value"><b>{_pct(rate.rate)}</b> {rate.runs} of {report.total_runs}'
        f"<small>{_range_text(rate)}</small></span></li>\n"
        for name, kind, rate, hatched in entries
    )
    return (
        f'<ol class="chart" aria-label="Share of all {report.total_runs} runs, with 95% range">\n'
        f'<li class="row axis" aria-hidden="true"><span></span>'
        f'<span class="ticks">{ticks}</span><span></span></li>\n{rows}</ol>'
    )


def _headline(report: Report) -> str:
    no_sdk = report.no_sdk
    competitors = [
        [_e(row.name), str(row.picks.runs), _pct(row.picks.rate), _range_text(row.picks)]
        for row in report.competitors
    ]
    verdicts = [
        [f"<code>{verdict}</code>", str(runs), _pct(runs / report.total_runs)]
        for verdict, runs in report.verdicts.items()
    ]
    return "\n".join(
        [
            '<section id="headline">\n<h2>Headline</h2>',
            f'<p class="big">{_pct(no_sdk.rate)}</p>',
            f"<p>of runs added no SDK: {no_sdk.runs} of {report.total_runs} "
            f"(95% range {_range_text(no_sdk)}). The chart shows that first, then one bar for "
            "every competitor, zero picks included.</p>",
            _chart(report),
            '<p class="note">Each bar is a share of all runs and the line is its 95% Wilson '
            "range. The hatched bar is <code>no_sdk</code>: no package was added except "
            "helpers, no competitor was imported and the diff added code. A competitor's bar "
            "counts the runs whose verdict is <code>picked</code>. Runs with any other "
            "verdict are in the totals.</p>",
            _table(
                "Pick rate by competitor",
                ["Competitor", "Picks", "Share of runs", "95% range"],
                competitors,
                {1, 2},
            ),
            _table("Verdicts of all runs", ["Verdict", "Runs", "Share of runs"], verdicts, {1, 2}),
            "</section>",
        ]
    )


def _tasks(report: Report) -> str:
    rows = [
        [
            str(task.index),
            f'{_e(task.prompt)}<small class="repeats">repeats in order: '
            f"{_e(', '.join(task.repeats)) or 'none'}</small>",
            str(task.runs),
            ", ".join(f"{_e(label)} {count}" for label, count in task.outcomes.items()) or "none",
        ]
        for task in report.tasks
    ]
    table = _table("What each task got", ["Task", "Prompt", "Runs", "Outcomes"], rows, {2})
    return f'<section id="tasks">\n<h2>By task</h2>\n{table}\n</section>'


def _builds(report: Report) -> str:
    rows = [
        [
            f"<code>{build.verdict}</code>",
            str(build.runs),
            str(build.built),
            str(build.passed),
            "no runs" if build.pass_rate is None else _pct(build.pass_rate),
        ]
        for build in report.builds
    ]
    table = _table(
        "npm run build, run once at the end of each run",
        ["Verdict", "Runs", "With a build", "Passed", "Pass rate"],
        rows,
        {1, 2, 3, 4},
    )
    return f'<section id="builds">\n<h2>Build pass rate by verdict</h2>\n{table}\n</section>'


def _switches(report: Report) -> str:
    if not report.switches:
        body = "<p>No run installed one competitor and then replaced it with another.</p>"
    else:
        rows = [
            [
                f"<code>{_e(switch.run_id)}</code>",
                str(switch.task),
                str(switch.repeat),
                _e(switch.from_competitor),
                _e(switch.to_competitor),
            ]
            for switch in report.switches
        ]
        body = _table("Switch events", ["Run", "Task", "Repeat", "From", "To"], rows, {1, 2})
    return f'<section id="switches">\n<h2>Switch events</h2>\n{body}\n</section>'


def _methods_box(report: Report) -> str:
    methods = report.methods
    cost = f"${methods.total_cost_usd:.2f}, every attempt included"
    if methods.unreported_attempts:
        cost += f"; {methods.unreported_attempts} attempt(s) reported no cost"
    runs = f"{report.total_runs} of {methods.expected_runs}, from {methods.attempts} attempts"
    facts = [
        ("Agent", ", ".join(methods.agents)),
        ("Model", ", ".join(methods.models) or "not recorded"),
        ("Claude Code version", ", ".join(methods.claude_code_versions) or "not recorded"),
        ("Starter", methods.starter),
        ("Tasks and repeats", f"{methods.tasks} tasks x {methods.repeats} repeats"),
        ("Runs reported", runs),
        ("Helper packages", ", ".join(methods.helper_packages) or "none"),
        ("Total cost", cost),
        ("Ran", f"{methods.first_run} to {methods.last_run}"),
        ("Report written", methods.generated),
    ]
    items = "".join(f"<dt>{_e(name)}</dt><dd>{_e(value)}</dd>\n" for name, value in facts)
    return (
        f'<section id="methods">\n<h2>Methods</h2>\n<dl class="methods">\n{items}</dl>\n</section>'
    )


HATCH = (
    '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)"><rect width="6" height="6" class="hook"/>'
    '<line x1="0" y1="0" x2="0" y2="6" class="hatch-line"/></pattern></defs>'
)

STYLE = """\
:root {
  color-scheme: light;
  --ink: #14181d;
  --ink-2: #4a5560;
  --line: #d9dee3;
  --bar: #3d6fc4;
  --hook: #c4501d;
  --wash: #f4f6f8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #fff;
  color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 60rem; margin: 0 auto; padding: 2.5rem 1rem 4rem; }
h1 { margin: 0.2rem 0 0.4rem; font-size: 2rem; line-height: 1.2; }
h2 {
  margin: 3rem 0 0.9rem;
  padding-top: 1rem;
  border-top: 1px solid var(--line);
  font-size: 1.25rem;
}
p { margin: 0.6rem 0; }
code { font: 0.875em ui-monospace, "Cascadia Mono", Consolas, monospace; }
small { display: block; color: var(--ink-2); font-size: 0.8125rem; }
.eyebrow {
  margin: 0;
  color: var(--ink-2);
  font-size: 0.75rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.sub, .note { color: var(--ink-2); }
.big { margin: 0; font-size: 3.6rem; font-weight: 600; line-height: 1.05; }
table {
  width: 100%;
  margin: 1rem 0 1.6rem;
  border-collapse: collapse;
  font-size: 0.9375rem;
  font-variant-numeric: tabular-nums;
}
caption { padding-bottom: 0.4rem; color: var(--ink-2); text-align: left; font-size: 0.8125rem; }
th, td {
  padding: 0.5rem 0.9rem 0.5rem 0;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
thead th { color: var(--ink-2); font-weight: 600; }
tbody th { font-weight: 500; }
.num { text-align: right; }
.scroll { overflow-x: auto; }
.chart { display: grid; gap: 0.25rem; margin: 1.4rem 0; padding: 0; list-style: none; }
.row {
  display: grid;
  grid-template-columns: 9rem minmax(0, 1fr) 7.5rem;
  gap: 0.75rem;
  align-items: center;
}
.name { font-weight: 500; }
.name small { font-weight: 400; }
.hand .name { color: var(--hook); }
.value { font-variant-numeric: tabular-nums; }
.value b { font-size: 1.1rem; }
.plot { display: block; width: 100%; height: auto; color: var(--bar); }
.plot .grid { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
.plot .whisker { stroke: var(--ink); stroke-width: 2; vector-effect: non-scaling-stroke; }
.plot .hook { fill: var(--hook); }
.plot .hatch-line { stroke: #5b1d07; stroke-width: 2; }
.ticks {
  position: relative;
  display: block;
  height: 1.1rem;
  color: var(--ink-2);
  font-size: 0.75rem;
}
.ticks span { position: absolute; transform: translateX(-50%); }
.ticks span:first-child { transform: none; }
.repeats { margin-top: 0.25rem; font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }
.methods {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 0.4rem 1.5rem;
  margin: 0;
  padding: 1.2rem 1.4rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--wash);
}
.methods dt { color: var(--ink-2); }
.methods dd { margin: 0; }
@media (max-width: 40rem) {
  .big { font-size: 2.8rem; }
  .row {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: "name value" "plot plot";
    row-gap: 0.1rem;
  }
  .row .name { grid-area: name; }
  .row .value { grid-area: value; text-align: right; }
  .row .plot { grid-area: plot; }
  .row.axis { display: none; }
  .methods { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .methods dd { margin-bottom: 0.6rem; }
}
"""

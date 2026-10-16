"""One report per eval run: aggregates plus every golden's scores and reasons.

Writes <name>_<date>_<time>.md and .csv to eval_results/reports/.
Each row is (golden, {metric name: metric object}, final agent state or None).
"""

import csv
from datetime import datetime

from hiring_agent.evals.metrics import REPORTS_DIR, THRESHOLD

NO_PLAN_WARNING = "no plans to evaluate"  # DeepEval's reason when it finds no plan: a 1.0 that checked nothing


def _result(metric) -> dict:
    """Score, pass flag and reason of one metric, or the error if it did not run."""
    error = getattr(metric, "error", None)
    score = getattr(metric, "score", None)
    if error or score is None:
        return {"score": None, "passed": False, "reason": f"ERROR: {error or 'no score'}"}
    return {"score": score, "passed": score >= THRESHOLD, "reason": (metric.reason or "").strip()}


def _cell(r: dict) -> str:
    return "ERROR" if r["score"] is None else f"{r['score']:.2f} {'PASS' if r['passed'] else 'FAIL'}"


def _one_line(text: str) -> str:
    return " ".join(text.split()).replace("|", "/")


def print_summary(rows) -> None:
    """Print every metric (DeepEval's and our custom judges) for every golden to the terminal.

    DeepEval's own table at the end of a run only knows the trace metrics, so this
    is where Plan Quality and Plan Adherence become visible without opening the report.
    """
    print("\n" + "=" * 80 + "\nALL METRICS\n" + "=" * 80)
    for i, (golden, metrics, _) in enumerate(rows, 1):
        print(f"\n[{i}] {golden.input}")
        for metric_name, metric in metrics.items():
            r = _result(metric)
            print(f"  {metric_name:<32} {_cell(r)}")
            print(f"      {_one_line(r['reason'])}")


def write_report(rows, name: str, meta: dict) -> str:
    """Write the md + csv report and return the markdown path."""
    results = [(golden, {m: _result(obj) for m, obj in metrics.items()}, state) for golden, metrics, state in rows]
    metric_names = list(results[0][1])
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- markdown ----
    md = [f"# Agent Eval report — {datetime.now():%d %b %Y, %H:%M}", ""]
    md += [f"- **{k}:** {v}" for k, v in meta.items()]
    md += [f"- **Goldens:** {len(results)}", f"- **Pass threshold:** {THRESHOLD}", ""]

    md += ["## Aggregate", "", "| Metric | Average score | Passed |", "|---|---|---|"]
    for m in metric_names:
        scored = [r[m] for _, r, _ in results if r[m]["score"] is not None]
        avg = sum(x["score"] for x in scored) / len(scored) if scored else float("nan")
        md.append(f"| {m} | {avg:.2f} | {sum(x['passed'] for x in scored)}/{len(results)} |")

    md += ["", "### Pass rate by difficulty", "", "| Difficulty | " + " | ".join(metric_names) + " |",
           "|---|" + "---|" * len(metric_names)]
    for level in ("easy", "medium", "difficult"):
        group = [r for g, r, _ in results if (g.additional_metadata or {}).get("difficulty") == level]
        if group:
            md.append(f"| {level} ({len(group)}) | " + " | ".join(f"{sum(r[m]['passed'] for r in group)}/{len(group)}" for m in metric_names) + " |")

    warnings = [(i, m) for i, (_, r, _) in enumerate(results, 1) for m in metric_names if NO_PLAN_WARNING in r[m]["reason"].lower()]
    if warnings:
        md += ["", "## Warnings", ""]
        md += [f"- Golden {i}, {m}: the judge found no plan in the trace, so this score checked nothing." for i, m in warnings]

    md += ["", "## Results per golden", "", "| # | Difficulty | Task | " + " | ".join(metric_names) + " |",
           "|---|---|---|" + "---|" * len(metric_names)]
    for i, (g, r, _) in enumerate(results, 1):
        diff = (g.additional_metadata or {}).get("difficulty", "")
        md.append(f"| {i} | {diff} | {_one_line(g.input)} | " + " | ".join(_cell(r[m]) for m in metric_names) + " |")

    md += ["", "## Details", ""]
    for i, (g, r, state) in enumerate(results, 1):
        md += [f"### {i}. {g.input}", "", f"*Difficulty: {(g.additional_metadata or {}).get('difficulty', '')}*", ""]
        if state:
            md += ["**Plan:**", ""] + [f"{n}. {s}" for n, s in enumerate(state["plan"], 1)]
            md += ["", f"**Agent response:** {_one_line(state['response'])}", ""]
        for m in metric_names:
            md += [f"**{m}: {_cell(r[m])}**", "", f"- Reason: {_one_line(r[m]['reason'])}", ""]

    md_path = REPORTS_DIR / f"{name}_{stamp}.md"
    md_path.write_text("\n".join(md))

    # ---- csv: one row per golden ----
    header = ["#", "difficulty", "type", "task"] + [f"{m} {col}" for m in metric_names for col in ("score", "passed", "reason")]
    with open(md_path.with_suffix(".csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for i, (g, r, _) in enumerate(results, 1):
            meta_g = g.additional_metadata or {}
            row = [i, meta_g.get("difficulty", ""), meta_g.get("type", ""), g.input]
            for m in metric_names:
                row += [r[m]["score"], r[m]["passed"], _one_line(r[m]["reason"])]
            writer.writerow(row)
    return str(md_path)

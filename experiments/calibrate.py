"""Calibrate the message-quality judge against the hand labels.

For every notice labelled in the notice-labels queue, run a judge on the same trace and compare,
question by question: agreement, Cohen's kappa (agreement corrected for chance) and the confusion
counts. Accepted when every question reaches THRESHOLD, fixed in the spec before any labels
existed. Exits 1 when not accepted. Rerun after any change to the judge prompt or model.

    uv run --env-file deploy/.env python -m experiments.calibrate --judge stand-in
"""

import argparse
from typing import Any

from experiments.gate import publish
from pricing_watch.evals.judge import DIMENSIONS, make_judge
from pricing_watch.evals.loader import fetch_trace

THRESHOLD = 0.6
Labels = dict[str, dict[str, bool]]  # trace id -> question -> passed


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for (human, judge) pass/fail pairs; None when every label is the same,
    where agreement by chance is total and kappa is undefined."""
    n = len(pairs)
    observed = sum(h == j for h, j in pairs) / n
    human_pass, judge_pass = sum(h for h, _ in pairs) / n, sum(j for _, j in pairs) / n
    chance = human_pass * judge_pass + (1 - human_pass) * (1 - judge_pass)
    return None if chance == 1 else (observed - chance) / (1 - chance)


def labels(client: Any) -> Labels:
    """The hand labels: annotation scores under the judge's own score names."""
    found: Labels = {}
    for d in DIMENSIONS:
        cursor = None
        while True:
            page = client.api.scores.get_many(name=f"judge_{d}", source="ANNOTATION", limit=100,
                                              cursor=cursor, fields="core,subject")  # fmt: skip
            for s in page.data:
                found.setdefault(s.subject.id, {})[d] = bool(s.value)
            cursor = page.meta.cursor
            if not cursor or not page.data:
                break
    return found


def report(human: Labels, judged: Labels) -> tuple[bool, str]:
    rows, accepted = [], True
    for d in DIMENSIONS:
        pairs = [
            (h[d], judged[t][d]) for t, h in human.items() if d in h and d in judged.get(t, {})
        ]
        if not pairs:
            continue
        k = kappa(pairs)
        both_pass = sum(h and j for h, j in pairs)
        both_fail = sum(not h and not j for h, j in pairs)
        judge_lenient = sum(not h and j for h, j in pairs)  # you failed it, the judge passed it
        judge_strict = sum(h and not j for h, j in pairs)
        ok = k is not None and k >= THRESHOLD
        accepted &= ok
        verdict = (
            "ok" if ok else "undefined: all labels alike" if k is None else f"below {THRESHOLD}"
        )
        agree = (both_pass + both_fail) / len(pairs)
        shown = "-" if k is None else f"{k:.2f}"
        rows.append(f"| {d} | {len(pairs)} | {agree:.0%} | {shown} | {both_pass} / {both_fail} | "
                    f"{judge_lenient} / {judge_strict} | {verdict} |")  # fmt: skip
    accepted &= bool(rows)
    head = ["| Question | Labels | Agreement | κ | Both pass / both fail "
            "| Judge passed your fail / failed your pass | Verdict |",
            "| --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
    table = "\n".join([*head, *rows]) + f"\n\n{'Accepted' if accepted else 'Not accepted'} " \
            f"(κ ≥ {THRESHOLD} on every question)\n"  # fmt: skip
    return accepted, table


def main() -> None:
    from pricing_watch.tracing.langfuse_tracer import langfuse_client

    parser = argparse.ArgumentParser(prog="experiments.calibrate")
    parser.add_argument("--judge", choices=["stand-in", "claude", "gemini"], default="stand-in",
                        help="stand-in: rules, free; claude, gemini: a call each")  # fmt: skip
    args = parser.parse_args()
    client = langfuse_client()
    judge = make_judge(args.judge, client)
    assert judge is not None
    human = labels(client)
    if not human:
        raise SystemExit("no hand labels yet: label the notice-labels queue in Langfuse first")
    judged: Labels = {}
    for trace_id in human:
        verdicts = judge.run(fetch_trace(client, trace_id))
        if verdicts:  # a judge answer that didn't parse is left out, and counted below
            judged[trace_id] = {d: s.value == 1.0 for d, s in verdicts.items()}
    accepted, table = report(human, judged)
    table = (
        f"Judge {judge.name} on {len(human)} labelled notices "
        f"({len(human) - len(judged)} without a usable judge answer)\n\n{table}"
    )
    publish(table)
    print(table)
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

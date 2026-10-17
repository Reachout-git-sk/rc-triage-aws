"""Score the classifier against the synthetic ground truth (data/truth.csv).

  python -m triage.evaluate --data data

Prints accuracy per category, the confusion pairs, the unknown share, and whether
the black-hole detector flagged the planted node. Only possible because the data is
synthetic: on a real cluster nobody hands you the true cause of every failure.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from triage.pipeline import analyze


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--data", type=Path, default=Path("data"))
    args = p.parse_args(argv)

    manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
    truth = {}
    with (args.data / "truth.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            truth[row["job_id"]] = row

    # All sacct files as one window.
    with tempfile.TemporaryDirectory() as tmp:
        combined = Path(tmp) / "sacct.txt"
        files = sorted((args.data / "sacct").glob("*.txt"))
        with combined.open("w", encoding="utf-8") as out:
            for i, f in enumerate(files):
                lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
                out.writelines(lines if i == 0 else lines[1:])
        end = datetime.fromisoformat(manifest["end"]).replace(tzinfo=UTC)
        t0 = time.perf_counter()
        result = analyze(
            combined,
            args.data / "logs" / "jobs",
            args.data / "logs" / "nodes",
            "evaluate",
            end - timedelta(days=manifest["days"]),
            end,
        )
        secs = time.perf_counter() - t0

    correct: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    confusion: Counter[tuple[str, str]] = Counter()
    for r in result.records:
        want = truth[r["job_id"]]["category"]
        seen[want] += 1
        if r["category"] == want and r["owner"] == truth[r["job_id"]]["owner"]:
            correct[want] += 1
        else:
            confusion[(want, r["category"])] += 1

    total = sum(seen.values())
    print(f"jobs: {len(result.jobs):,}  failed: {total:,}  analysis: {secs:.1f}s")
    print(f"overall accuracy (category and owner): {sum(correct.values()) / total:.1%}")
    for cat in sorted(seen, key=lambda c: -seen[c]):
        print(f"  {cat:<24} {correct[cat]:>5}/{seen[cat]:<5} {correct[cat] / seen[cat]:.0%}")
    if confusion:
        print("mistakes (truth -> predicted):")
        for (want, got), n in confusion.most_common():
            print(f"  {want} -> {got}: {n}")
    unknown = sum(1 for r in result.records if r["owner"] == "human_review")
    print(f"unknown share: {unknown / total:.1%}")

    planted = manifest["incidents"]["blackhole"]["node"]
    flagged = [a.node for a in result.alerts]
    print(f"black-hole nodes flagged: {flagged} (planted: {planted})")
    for a in result.alerts:
        print(
            f"  {a.node}: {a.failed_jobs}/{a.total_jobs} failed, {a.distinct_users} users, "
            f"median {a.median_elapsed_s}s, categories {a.details['categories']}"
        )
    return 0 if planted in flagged else 1


if __name__ == "__main__":
    sys.exit(main())

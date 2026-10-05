"""Recombine sharded replay output into one dataset and one report.

Kept separate from replay.py so a merge can be rerun without re-replaying, and so a
failed shard is visible as a missing file rather than as a quietly smaller sample. The
shard count is asserted rather than inferred: a merge that silently proceeds on 2 of 3
shards produces a result that looks fine and is measured on two thirds of the intended
universe.
"""
import argparse
import csv
import glob
import os

from research import replay as replay_mod


def merge_trades(out_dir, expected_shards=None):
    paths = sorted(glob.glob(os.path.join(out_dir, "trades_shard*.csv")))
    if expected_shards is not None and len(paths) != expected_shards:
        raise SystemExit(
            f"expected {expected_shards} shard files, found {len(paths)}: {paths}\n"
            f"a partial merge would report a result measured on a smaller universe "
            f"than intended - rerun the missing shard(s) first")

    rows = []
    for path in paths:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            rows.extend(list(csv.DictReader(handle)))

    # Types come from replay.TRADE_FIELD_TYPES, which lives beside TRADE_FIELDS so the
    # two cannot drift. They already had: the hand-written lists this replaced left
    # exit_price, exit_ts and decision_index as strings, invisibly, because nothing
    # happened to do arithmetic on them yet.
    for row in rows:
        replay_mod.coerce_row(row)
    return rows, paths


def merge_rejects(out_dir):
    tally = {}
    for path in sorted(glob.glob(os.path.join(out_dir, "rejects_shard*.csv"))):
        with open(path, "r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = row["reason"]
                tally[key] = tally.get(key, 0) + int(row["count"])
    return tally


def main(argv=None):
    parser = argparse.ArgumentParser(description="Merge sharded replay output.")
    parser.add_argument("--out", default="phase1")
    parser.add_argument("--shards", type=int, default=None,
                        help="assert this many shard files are present")
    args = parser.parse_args(argv)

    rows, paths = merge_trades(args.out, args.shards)
    rejects = merge_rejects(args.out)
    merged = os.path.join(args.out, "trades.csv")
    replay_mod.write_csv(merged, rows)

    print(f"merged {len(paths)} shards -> {len(rows)} candidates -> {merged}")
    replay_mod.report(rows, rejects, label="PHASE 1/2 (merged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

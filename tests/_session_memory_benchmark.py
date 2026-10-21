"""Reproducible public-path counters using owned temporary synthetic databases.

Run with the repository's locked environment. Timing includes tracemalloc and
counter instrumentation; it is diagnostic data, not a latency/SLO guarantee.
"""

import argparse
import gc
import json
import sys
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import BaseModel, TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from lock_support import CRED, A  # noqa: E402
from test_lockable_passages import acquire  # noqa: E402
from test_scoped_runtime_validation import grow, measure  # noqa: E402
from test_world_containers import BOX, container_data  # noqa: E402
from world_support import setup  # noqa: E402

from trpg_state_machine import PutItem, TakeItem, TrustedContext, TypedCommand  # noqa: E402
from trpg_state_machine.commands import OPERATIONS  # noqa: E402


@contextmanager
def construction_counts():
    counts = Counter()
    load = json.loads
    validate = BaseModel.model_validate_json.__func__
    adapt = TypeAdapter.validate_json

    def loads(*args, **kwargs):
        frame = sys._getframe(1)
        label = Path(frame.f_code.co_filename).name + ":" + frame.f_code.co_name
        counts["json.loads:" + label] += 1
        return load(*args, **kwargs)

    def model(cls, *args, **kwargs):
        counts["model_validate_json:" + cls.__name__] += 1
        return validate(cls, *args, **kwargs)

    def adapter(self, *args, **kwargs):
        counts["type_adapter_validate_json"] += 1
        return adapt(self, *args, **kwargs)

    with (
        patch.object(json, "loads", loads),
        patch.object(BaseModel, "model_validate_json", classmethod(model)),
        patch.object(TypeAdapter, "validate_json", adapter),
    ):
        yield counts


def sample(growth, work, cycles):
    folder = work / str(growth)
    folder.mkdir(exist_ok=False)
    context = TrustedContext(
        "authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant")
    )
    sm, sid, package, _ = setup(folder, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        if growth:
            grow(sm, sid, growth)
        state = sm.read_session(sid, package, context=context)
        version, serial = state.world_version, 0
        actor = state.pc_actor_id

        def cycle(count):
            nonlocal version, serial
            elapsed = []
            for _ in range(count):
                for payload in (PutItem(A, CRED, BOX, "0,0"), TakeItem(A, CRED, BOX, "0,0")):
                    serial += 1
                    kind = next(k for k, cls in OPERATIONS.items() if type(payload) is cls)
                    command = TypedCommand(sid, f"metric.{serial}", actor, kind, payload, version)
                    start = time.perf_counter_ns()
                    receipt = sm.submit_command(command, package, context=context)
                    elapsed.append((time.perf_counter_ns() - start) / 1e6)
                    assert receipt.status == "committed", receipt
                    version = receipt.world_version
            return elapsed

        cycle(2)
        sm.read_session(sid, package, context=context)
        sm.get_object(sid, actor, BOX, package, context=context)
        previous = dict(sm._store.memory.stats)
        with measure(sm) as (hot, _), construction_counts() as hot_models:
            for _ in range(10):
                sm.get_object(sid, actor, BOX, package, context=context)
        hot_stats = {k: sm._store.memory.stats[k] - previous[k] for k in previous}
        gc.collect()
        tracemalloc.start()
        previous = dict(sm._store.memory.stats)
        with measure(sm) as (sql, _), construction_counts() as models:
            durations = cycle(cycles)
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stats = {k: sm._store.memory.stats[k] - previous[k] for k in previous}
        # Separate deterministic VM measurement from latency measurement.
        vm_steps = 0

        def progress():
            nonlocal vm_steps
            vm_steps += 1
            return 0

        sm._conn.set_progress_handler(progress, 1)
        try:
            cycle(2)
        finally:
            sm._conn.set_progress_handler(None, 0)
        return {
            "unrelated_growth": growth,
            "hot_reads": 10,
            "hot_sql": dict(hot),
            "hot_runtime": hot_stats,
            "commands": 2 * cycles,
            "sql": dict(sql),
            "runtime": stats,
            "hot_construction": dict(hot_models),
            "construction": dict(models),
            "latency_ms": {"median": sorted(durations)[len(durations) // 2], "max": max(durations)},
            "payload_bytes_after": sm._store.memory.bytes,
            "traced_new_retained_bytes": retained,
            "traced_peak_bytes": peak,
            "sqlite_vm_steps_for_4_commands": vm_steps,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--growth", type=int, default=150)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".test-tmp" / "session-memory-measurements-final.json"
    )
    args = parser.parse_args()
    if args.growth < 1 or args.cycles < 1:
        parser.error("growth and cycles must be positive")
    temp = ROOT / ".test-tmp"
    temp.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="session-memory-metrics-", dir=temp) as work:
        result = [sample(n, Path(work), args.cycles) for n in (0, args.growth)]
    args.output.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

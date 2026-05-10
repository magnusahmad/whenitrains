import threading
import time
import unittest

from whenitrains.execution_scheduler import CandidateAction, ExecutionScheduler


class ExecutionSchedulerTests(unittest.TestCase):
    def test_independent_candidates_run_concurrently(self):
        started = threading.Event()
        both_started = threading.Event()
        release = threading.Event()
        calls = []
        starts = 0
        starts_lock = threading.Lock()

        def action(name):
            def run():
                nonlocal starts
                calls.append(f"{name}-start")
                with starts_lock:
                    starts += 1
                    if starts == 1:
                        started.set()
                    if starts == 2:
                        both_started.set()
                release.wait(timeout=1)
                calls.append(f"{name}-done")
                return name

            return run

        scheduler = ExecutionScheduler(max_workers=2)
        first = CandidateAction("a", conflict_keys=frozenset({"token:yes26"}), run=action("a"))
        second = CandidateAction("b", conflict_keys=frozenset({"token:yes27"}), run=action("b"))

        thread = threading.Thread(target=lambda: scheduler.run([first, second]))
        thread.start()
        started.wait(timeout=1)
        both_started.wait(timeout=1)
        release.set()
        thread.join(timeout=1)

        self.assertIn("a-start", calls)
        self.assertIn("b-start", calls)
        self.assertLess(calls.index("b-start"), calls.index("a-done"))

    def test_conflicting_candidates_are_serialized_in_input_order(self):
        calls = []

        def action(name):
            def run():
                calls.append(name)
                time.sleep(0.01)
                return name

            return run

        scheduler = ExecutionScheduler(max_workers=2)
        results = scheduler.run(
            [
                CandidateAction(
                    "first",
                    conflict_keys=frozenset({"token:yes26", "risk:daily"}),
                    run=action("first"),
                ),
                CandidateAction(
                    "second",
                    conflict_keys=frozenset({"token:yes26"}),
                    run=action("second"),
                ),
                CandidateAction(
                    "third",
                    conflict_keys=frozenset({"token:yes27"}),
                    run=action("third"),
                ),
            ]
        )

        self.assertLess(calls.index("first"), calls.index("second"))
        self.assertEqual([result.key for result in results], ["first", "second", "third"])

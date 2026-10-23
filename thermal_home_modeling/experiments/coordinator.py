"""Shared local execution coordination for commercial and residential cases."""
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue


def coordinate_cases(cases, execute, max_workers, on_event, on_tick):
    """Worker entry/exit events; all callbacks run on the caller thread.

    Submit only a bounded number of jobs. On interruption, queued futures are
    cancelled; active subprocess workflows finish and preserve their own records.
    Resubmission can recover those records through validated cache reuse.
    """
    events = Queue()
    def worker(index):
        events.put(("started", index, None))
        result = execute(index)
        events.put(("finished", index, result))
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="energyplus-case")
    futures = {}
    next_index = 0
    try:
        while next_index < min(max_workers, len(cases)):
            futures[next_index] = executor.submit(worker, next_index)
            next_index += 1
        finished = 0
        while finished < len(cases):
            try:
                event, index, result = events.get(timeout=0.2)
            except Empty:
                for future in futures.values():
                    if future.done():
                        future.result()
                on_tick()
                continue
            on_event(event, index, result)
            if event == "finished":
                futures.pop(index).result()
                finished += 1
                if next_index < len(cases):
                    futures[next_index] = executor.submit(worker, next_index)
                    next_index += 1
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

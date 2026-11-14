#!/usr/bin/env python3
import queue
import threading


class AssetEventQueue:
    def __init__(self, callback, max_pending=2048):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.callback = callback
        self.queue = queue.Queue(maxsize=max_pending)
        self.lock = threading.Lock()
        self.stopping = False
        self.events_received = 0
        self.events_processed = 0
        self.events_coalesced = 0
        self.events_dropped = 0
        self.processing_errors = 0
        self.worker = threading.Thread(
            target=self._run,
            name="RecogAssetEvents",
            daemon=True
        )
        self.worker.start()

    def submit_batch(self, paths):
        unique_paths = list(dict.fromkeys(paths))
        with self.lock:
            self.events_received += len(paths)
            self.events_coalesced += len(paths) - len(unique_paths)
            if self.stopping:
                self.events_dropped += len(unique_paths)
                return 0

            accepted = 0
            for path in unique_paths:
                try:
                    self.queue.put_nowait(path)
                    accepted += 1
                except queue.Full:
                    self.events_dropped += 1
            return accepted

    def _run(self):
        while True:
            path = self.queue.get()
            try:
                if path is None:
                    return
                try:
                    self.callback(path)
                except Exception as exc:
                    with self.lock:
                        self.processing_errors += 1
                    print(
                        f"[EVENT-WORKER] Failed to process {path}: {exc}",
                        flush=True
                    )
                else:
                    with self.lock:
                        self.events_processed += 1
            finally:
                self.queue.task_done()

    def get_stats(self):
        with self.lock:
            return {
                "events_received": self.events_received,
                "events_processed": self.events_processed,
                "events_coalesced": self.events_coalesced,
                "events_dropped": self.events_dropped,
                "event_processing_errors": self.processing_errors,
                "event_queue_depth": self.queue.qsize()
            }

    def stop(self):
        with self.lock:
            enqueue_stop = not self.stopping
            self.stopping = True
        if enqueue_stop:
            self.queue.put(None)
        self.queue.join()
        self.worker.join()

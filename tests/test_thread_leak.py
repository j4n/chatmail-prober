"""Non-mocking tests for receiver-thread cleanup in Pinger.receive().

These tests exercise real threading.Thread and queue.Queue objects; no
mocking of threading primitives.  They exist to catch a regression where
receiver threads blocked indefinitely on queue.get() after receive() returned,
causing thread accumulation across probe rounds and eventual OOM.

The fix: receiver_thread polls with account_queue.get(timeout=1.0) and checks
stop_event, so the finally block in receive() can stop_event.set() + join().
"""
import argparse
import queue
import threading
import time

import pytest

from cmping import Pinger


# ---------------------------------------------------------------------------
# Minimal stubs -- just enough interface for Pinger to function without a
# real deltachat-rpc-server.  All threading primitives are real.
# ---------------------------------------------------------------------------

class _Rpc:
    """RPC stub backed by real queue.Queue instances."""

    def __init__(self):
        self._queues = {}

    def get_queue(self, account_id):
        if account_id not in self._queues:
            self._queues[account_id] = queue.Queue()
        return self._queues[account_id]


class _Account:
    def __init__(self, rpc, account_id, relay):
        self._rpc = rpc
        self.id = account_id
        self._relay = relay

    def get_config(self, key):
        return f"user{self.id}@{self._relay}"


class _Group:
    def send_text(self, text):
        pass  # send_pings calls this; no-op is fine


def _make_pinger(count=1, interval=0.0):
    """Create a Pinger with stub accounts and an already-past deadline so
    receive() exits immediately without needing real network events.

    interval=0.0 is fine for tests that only check receiver_thread cleanup,
    but causes a race for _send_thread: on multi-core systems the thread can
    complete its loop body before the main thread sets p.deadline, then reach
    `if self.deadline is None: time.sleep(60)` with deadline still None.
    Use interval=0.1 when _send_thread accumulation would fail the test.
    """
    rpc = _Rpc()
    args = argparse.Namespace(count=count, interval=interval, verbose=0, numrecipients=1)
    sender = _Account(rpc, 1, "src.example")
    receiver = _Account(rpc, 2, "dst.example")
    p = Pinger(args, sender, _Group(), [receiver])
    # Deadline in the past: send_pings exits immediately, receive() breaks at
    # the top of its while loop and the finally block runs synchronously.
    p.deadline = time.time() - 1.0
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReceiverThreadCleanup:
    """Verify that Pinger.receive() cleans up its receiver threads on exit."""

    def test_threads_exit_after_deadline(self):
        """All receiver threads must be joined before receive() returns."""
        p = _make_pinger()
        before = threading.active_count()

        list(p.receive())

        # The finally block joins with timeout=2s, so threads should be gone
        # by the time list() returns.  Allow +1 for _send_thread which may
        # briefly outlive receive() (it checks deadline at loop top).
        assert threading.active_count() <= before + 1, (
            f"Receiver threads still alive after receive() returned: "
            f"was {before}, now {threading.active_count()}"
        )

    def test_no_thread_accumulation_across_rounds(self):
        """Thread count must not grow with the number of probe rounds.

        Before the fix, each receive() left one blocked receiver_thread alive
        per round, so N rounds -> N extra threads.

        Uses interval=0.1 so _send_thread's sleep(interval) gives the main
        thread time to set p.deadline before the post-loop `if deadline is None`
        check, avoiding the multi-core race that would strand _send_thread in
        its 60-second fallback sleep.
        """
        # Warm-up: let any setup threads from import settle.
        p0 = _make_pinger(interval=0.1)
        list(p0.receive())
        time.sleep(0.2)
        baseline = threading.active_count()

        N = 8
        for _ in range(N):
            p = _make_pinger(interval=0.1)
            list(p.receive())
            # _send_thread exits within ~0.1s (its interval sleep), well before
            # receive()'s ~1s receiver_thread poll completes.  join() is a
            # belt-and-suspenders check that mirrors what perform_ping does.
            p._send_thread.join(timeout=2.0)

        time.sleep(0.2)
        after = threading.active_count()

        assert after <= baseline + 2, (
            f"Thread leak detected: baseline={baseline}, after {N} rounds={after} "
            f"(expected <={baseline + 2})"
        )

    def test_multiple_receivers_all_exit(self):
        """Each receiver gets its own thread; all must exit cleanly."""
        rpc = _Rpc()
        args = argparse.Namespace(count=1, interval=0.0, verbose=0, numrecipients=3)
        sender = _Account(rpc, 1, "src.example")
        receivers = [_Account(rpc, i, "dst.example") for i in range(2, 5)]
        p = Pinger(args, sender, _Group(), receivers)
        p.deadline = time.time() - 1.0

        before = threading.active_count()
        list(p.receive())

        # 3 receiver threads must all be gone (+1 tolerance for _send_thread).
        assert threading.active_count() <= before + 1, (
            f"Some of the 3 receiver threads leaked: was {before}, "
            f"now {threading.active_count()}"
        )

    def test_stop_event_unblocks_polling_thread(self):
        """Directly verify the stop_event mechanism.

        A thread that polls with get(timeout=1.0) + stop_event check must
        exit within ~1.5s of stop_event.set(), even when the queue is empty.
        """
        q = queue.Queue()
        stop = threading.Event()
        results = []

        def polling_thread():
            while not stop.is_set():
                try:
                    q.get(timeout=1.0)
                except queue.Empty:
                    continue
            results.append("exited")

        t = threading.Thread(target=polling_thread, daemon=True)
        t.start()
        assert t.is_alive()

        stop.set()
        t.join(timeout=2.0)

        assert not t.is_alive(), "Thread did not exit within 2s of stop_event.set()"
        assert results == ["exited"]

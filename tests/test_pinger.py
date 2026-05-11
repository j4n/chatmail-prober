"""Tests for chatmail_prober.pinger.Pinger receive/cleanup behaviour.

These tests exercise real threading.Thread and queue.Queue objects; no
mocking of threading primitives. They exist to catch a regression where
receiver threads blocked indefinitely on queue.get() after receive()
returned, causing thread accumulation across probe rounds and eventual OOM.

The vendored Pinger uses a single-receiver inline loop (no thread pool),
so "thread leak" here refers only to the _send_thread daemon.
"""
import queue
import threading
import time

from chatmail_prober.probe import Pinger

#
# Minimal stubs -- just enough interface for Pinger to function without a
# real deltachat-rpc-server.  All threading primitives are real.
#

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

    def create_contact(self, other):
        return _Contact()


class _Contact:
    def create_chat(self):
        return _Chat()


class _Chat:
    def send_text(self, text):
        pass  # send_pings calls this; no-op is fine


def _make_pinger(count=1, interval=0.0):
    """Create a started Pinger with stub accounts and an already-past
    deadline so receive() exits immediately without needing real network
    events.

    interval=0.0 is fine for tests that only check cleanup; a small
    timeout < 0 makes both threads see deadline-already-elapsed.
    """
    rpc = _Rpc()
    sender = _Account(rpc, 1, "src.example")
    receiver = _Account(rpc, 2, "dst.example")
    # Negative timeout -> monotonic deadline in the past for both threads,
    # so send_pings exits at the top of its loop and receive() breaks
    # immediately. Equivalent to the old `p.deadline = time.time() - 1.0`
    # but without cross-thread mutation.
    p = Pinger(sender, receiver, count, interval, timeout=-1.0)
    p.start()
    return p


#
# Tests
#

class TestReceiverCleanup:
    """Verify that Pinger.receive() cleans up on exit."""

    def test_threads_exit_after_deadline(self):
        """_send_thread must exit shortly after receive() returns."""
        p = _make_pinger()
        before = threading.active_count()

        list(p.receive())

        # Allow +1 for _send_thread which may briefly outlive receive().
        assert threading.active_count() <= before + 1, (
            f"Threads still alive after receive() returned: "
            f"was {before}, now {threading.active_count()}"
        )

    def test_no_thread_accumulation_across_rounds(self):
        """Thread count must not grow with the number of probe rounds.

        Uses interval=0.1 so _send_thread's sleep(interval) is interrupted
        by the past-deadline check on the next loop iteration rather than
        racing the main thread to teardown.
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
            p._send_thread.join(timeout=2.0)

        time.sleep(0.2)
        after = threading.active_count()

        assert after <= baseline + 2, (
            f"Thread leak detected: baseline={baseline}, after {N} rounds={after} "
            f"(expected <={baseline + 2})"
        )


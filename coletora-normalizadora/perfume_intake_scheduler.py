"""Bounded sliding admission. No sends, acknowledgements, or persistent state here."""
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def admitted_results(initial, collect, process, *, max_workers=3, max_messages=24,
                     admission_seconds: float = 120, poll_seconds: float = 1):
    """Yield ready (message, result), admitting new messages into available slots.

    Caller alone acknowledges after delivery. IDs admitted in this invocation
    remain excluded even if the tap repeats them. Exit when drained; do not keep
    a process alive across operational windows. Bound admissions, never kill work.
    """
    pending = []
    seen = set()
    active = {}
    started = time.monotonic()
    next_poll = started + poll_seconds
    admitted = 0

    def remember(messages):
        for msg in messages:
            mid = str(msg.get('messageId') or '')
            if not mid or mid in seen:
                continue
            seen.add(mid)
            pending.append((msg, time.monotonic()))

    remember(initial)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        def fill_slots():
            nonlocal admitted
            while (pending and len(active) < max_workers and admitted < max_messages
                   and time.monotonic() - started < admission_seconds):
                msg, observed = pending.pop(0)
                active[pool.submit(process, msg, observed)] = msg
                admitted += 1

        while True:
            now = time.monotonic()
            can_admit = admitted < max_messages and now - started < admission_seconds
            if can_admit and (now >= next_poll or (not pending and len(active) < max_workers)):
                remember(collect())
                next_poll = time.monotonic() + poll_seconds
            fill_slots()
            if not active:
                return
            done, _ = wait(active, timeout=poll_seconds, return_when=FIRST_COMPLETED)
            ready = [(active.pop(future), future) for future in done]
            # The consumer serializes real sends and sleeps between messages.
            # Refill freed slots BEFORE yielding, not after those sends finish.
            # Same worker/admission caps; acknowledgements stay with the sender.
            fill_slots()
            for msg, future in ready:
                yield msg, future.result()

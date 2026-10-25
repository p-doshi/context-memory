"""The tunnel watcher republishes the site between daemon passes."""

import threading
import time

from context_memory.server import daemon


def test_tunnel_watcher_publishes_between_passes(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_site_publish", lambda s: calls.append(1) or {})
    stop = threading.Event()
    t = daemon.watch_tunnel(settings, stop, every=0.01)
    deadline = time.time() + 2
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(1)
    assert len(calls) >= 3 and not t.is_alive()


def test_pass_and_watcher_never_publish_at_once(settings, monkeypatch):
    inside, overlap = [0], []

    def slow(_):
        inside[0] += 1
        overlap.append(inside[0])
        time.sleep(0.05)
        inside[0] -= 1
        return {}
    monkeypatch.setattr(daemon, "_site_publish", slow)
    threads = [threading.Thread(target=daemon.site_publish_due, args=(settings,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(overlap) == 1

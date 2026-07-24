import threading
import time

from services.llm.request_gate import PriorityRequestGate


def test_core_waiter_precedes_queued_enrichment_after_active_request():
    gate = PriorityRequestGate()
    active = threading.Event()
    release = threading.Event()
    order = []

    def first_enrichment():
        with gate.admit("enrichment"):
            active.set()
            release.wait(timeout=2)

    def queued_enrichment():
        active.wait(timeout=2)
        with gate.admit("enrichment"):
            order.append("enrichment")

    def core():
        active.wait(timeout=2)
        with gate.admit("core"):
            order.append("core")

    first = threading.Thread(target=first_enrichment)
    enrichment = threading.Thread(target=queued_enrichment)
    core_thread = threading.Thread(target=core)
    first.start()
    active.wait(timeout=2)
    enrichment.start()
    core_thread.start()
    time.sleep(0.02)
    release.set()
    first.join(timeout=2)
    enrichment.join(timeout=2)
    core_thread.join(timeout=2)

    assert order == ["core", "enrichment"]


def test_gate_reports_queue_wait_time():
    gate = PriorityRequestGate()
    active = threading.Event()
    release = threading.Event()
    waits = []

    def blocker():
        with gate.admit("core"):
            active.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=blocker)
    thread.start()
    active.wait(timeout=2)
    timer = threading.Timer(0.03, release.set)
    timer.start()
    with gate.admit("enrichment") as waited_ms:
        waits.append(waited_ms)
    timer.cancel()
    thread.join(timeout=2)

    assert waits[0] >= 20

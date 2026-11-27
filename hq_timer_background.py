"""Local worker lifecycle for private timer observations, never network delivery."""
from threading import Event, Thread

from customer_request_store import storage_ready
import hq_timer_alerts

INTERVAL_SECONDS = 60


def run(stop, logger):
    """Scan immediately, then wait interruptibly; a failed scan must not kill the worker."""
    first_success = True
    while not stop.is_set():
        try:
            hq_timer_alerts.scan(automatic=True)
            if first_success:
                logger.info("Private timer alert background scan operational")
                first_success = False
        except Exception as exc:
            # Never log customer records or exception details containing private data.
            logger.warning("Private timer alert scan failed (%s); retrying", type(exc).__name__)
        stop.wait(INTERVAL_SECONDS)


def start(worker):
    """Called after Gunicorn forks and initializes the application, not at import time."""
    if getattr(worker, "_hq_timer_alert_thread", None) is not None:
        return
    if not storage_ready():
        worker.log.warning("Private timer alert background scan disabled: persistent storage not configured")
        return
    stop = Event()
    thread = Thread(target=run, args=(stop, worker.log), name="hq-timer-alerts", daemon=True)
    worker._hq_timer_alert_stop = stop
    worker._hq_timer_alert_thread = thread
    thread.start()


def shutdown(worker):
    stop = getattr(worker, "_hq_timer_alert_stop", None)
    thread = getattr(worker, "_hq_timer_alert_thread", None)
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=1)

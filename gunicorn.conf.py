"""Web worker hooks; existing bind and worker count remain in Procfile."""

def post_worker_init(worker):
    from hq_timer_background import start
    start(worker)


def worker_exit(server, worker):
    from hq_timer_background import shutdown
    shutdown(worker)

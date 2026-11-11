import asyncio
from concurrent.futures import ThreadPoolExecutor


class TrackedExecutor:
    def __init__(self, name):
        self.name = name
        self.pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="ahead-" + name
        )
        self.jobs = set()
        self.closed = False

    @property
    def busy(self):
        return any((not job.done() for job in tuple(self.jobs)))

    async def run(self, function, *args):
        if self.closed:
            if self.busy:
                raise RuntimeError(
                    "Native work has not stopped after closing the previous execution"
                )
            self.pool = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="ahead-" + self.name
            )
            self.closed = False
        job = self.pool.submit(function, *args)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return await asyncio.wrap_future(job)

    async def close(self):
        jobs = list(self.jobs)
        for job in jobs:
            job.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.closed = True

import asyncio


class Subscription:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

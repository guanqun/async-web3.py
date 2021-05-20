import asyncio


class Subscription:
    def __init__(self, subscription_id: str, queue: asyncio.Queue):
        self.subscription_id = subscription_id
        self.queue = queue

    @property
    def id(self) -> str:
        return self.subscription_id

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

import abc
import asyncio
import websockets


class BaseTransport(abc.ABC):

    @abc.abstractmethod
    async def connect(self):
        pass

    @abc.abstractmethod
    async def send(self, data: bytes):
        pass

    @abc.abstractmethod
    async def receive(self) -> bytes:
        pass


class IPCTransport(BaseTransport):

    def __init__(self, local_ipc_path: str):
        self._local_ipc_path = local_ipc_path
        self._writer = None
        self._reader = None

    async def connect(self):
        self._reader, self._writer = await asyncio.open_unix_connection(self._local_ipc_path)

    async def send(self, data: bytes):
        self._writer.write(data)
        await self._writer.drain()

    async def receive(self) -> bytes:
        return await self._reader.readuntil()


class WebsocketTransport(BaseTransport):

    def __init__(self, websocket_uri: str):
        self._websocket_uri = websocket_uri
        self._ws = None

    async def connect(self):
        self._ws = await websockets.connect(self._websocket_uri)

    async def send(self, data: bytes):
        await self._ws.send(data)

    async def receive(self) -> bytes:
        return await self._ws.recv()


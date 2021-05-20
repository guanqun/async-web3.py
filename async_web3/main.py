import asyncio
import itertools
from typing import Optional, Dict, Any
import logging
import websockets
import json


class AsyncWeb3:
    logger = logging.getLogger("async_web3.AsyncWeb3")

    def __init__(self, websocket_uri: str):
        self.websocket_uri = websocket_uri

        self.rpc_counter = itertools.count(1)
        self.ws = None

        self._requests: Dict[int, asyncio.Future] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.websocket_uri)
        asyncio.get_event_loop().create_task(self.ws_process())

    async def is_connect(self):
        try:
            await self.ws_request("web3_clientVersion")
        except Exception:
            return False

        return True

    async def ws_request(self, method, params: Any = None):
        counter = next(self.rpc_counter)
        rpc_dict = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": counter,
        }
        encoded = json.dumps(rpc_dict).encode("utf-8")
        fut = asyncio.get_event_loop().create_future()
        self._requests[counter] = fut
        await self.ws.send(encoded)
        result = await fut
        del self._requests[counter]
        return result

    async def ws_subscribe(self):
        pass

    async def ws_process(self):
        async for msg in self.ws:
            jo = json.loads(msg)
            request_id = jo["id"]
            if request_id in self._requests:
                self._requests[request_id].set_result(jo["result"])

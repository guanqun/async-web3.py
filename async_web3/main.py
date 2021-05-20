import asyncio
import itertools
from typing import Optional, Dict, Any
import logging
import websockets
import json
from web3.types import Wei, Address

from .subscription import Subscription
from .methods import RPCMethod


class AsyncWeb3:
    logger = logging.getLogger("async_web3.AsyncWeb3")

    def __init__(self, websocket_uri: str):
        self.websocket_uri = websocket_uri

        self.rpc_counter = itertools.count(1)
        self.ws: websockets.WebSocketClientProtocol = None

        self._requests: Dict[int, asyncio.Future] = {}
        self._subscriptions: Dict[str, asyncio.Queue] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.websocket_uri)
        asyncio.get_event_loop().create_task(self.ws_process())

    async def is_connect(self):
        try:
            await self.ws_request("web3_clientVersion")
        except Exception:
            return False

        return True

    @property
    async def block_number(self) -> int:
        hex_block = await self.ws_request(RPCMethod.eth_blockNumber)
        # it's a hex block
        return int(hex_block, 16)

    @property
    async def gas_price(self) -> Wei:
        hex_wei = await self.ws_request(RPCMethod.eth_gasPrice)
        return Wei(int(hex_wei, 16))

    async def get_balance(self, address: Address) -> Wei:
        hex_wei = await self.ws_request(RPCMethod.eth_getBalance, [address])

    async def subscribe_block(self) -> Subscription:
        subscription_id = await self.ws_request(RPCMethod.eth_subscribe, ["newHeads"])

        queue = asyncio.Queue()
        self._subscriptions[subscription_id] = queue
        return Subscription(subscription_id, queue)

    async def unsubscribe(self, subscription: Subscription):
        response = await self.ws_request(RPCMethod.eth_unsubscribe, [subscription.id])
        assert response
        queue = self._subscriptions[subscription.id]
        del self._subscriptions[subscription.id]
        queue.task_done()

    async def ws_request(self, method, params: Any = None):
        request_id = next(self.rpc_counter)
        rpc_dict = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": request_id,
        }
        encoded = json.dumps(rpc_dict).encode("utf-8")
        fut = asyncio.get_event_loop().create_future()
        self._requests[request_id] = fut
        await self.ws.send(encoded)
        self.logger.debug(f"websocket outbound: {encoded}")
        result = await fut
        del self._requests[request_id]
        return result

    async def ws_process(self):
        async for msg in self.ws:
            self.logger.debug(f"websocket inbound: {msg}")
            j = json.loads(msg)
            if "method" in j and j["method"] == "eth_subscription":
                params = j["params"]
                subscription_id = params["subscription"]
                if subscription_id in self._subscriptions:
                    # TODO: maybe wrap this as block info?
                    self._subscriptions[subscription_id].put_nowait(params["result"])
            if "id" in j:
                request_id = j["id"]
                if request_id in self._requests:
                    self._requests[request_id].set_result(j["result"])

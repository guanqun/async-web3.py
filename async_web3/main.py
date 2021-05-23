import asyncio
import itertools
from typing import Optional, Dict, Any, Union, List
import logging
import websockets
import json
from web3 import Web3

from .subscription import Subscription
from .methods import RPCMethod
from .contract import DeployedContract
from .types import Wei, Address, HexString


def _format_block_identifier(block_identifier: Union[int, str, bytes]):
    if block_identifier is None:
        return 'latest'
    elif isinstance(block_identifier, int):
        return hex(block_identifier)
    else:
        return block_identifier


class AsyncWeb3:
    logger = logging.getLogger("async_web3.AsyncWeb3")

    def __init__(self, websocket_uri: str):
        self.websocket_uri = websocket_uri

        self.rpc_counter = itertools.count(1)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

        self._requests: Dict[int, asyncio.Future] = {}
        self._subscriptions: Dict[str, asyncio.Queue] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.websocket_uri)
        asyncio.get_event_loop().create_task(self._ws_process())

    async def is_connect(self):
        try:
            await self.client_version
        except Exception:
            return False

        return True

    @property
    async def client_version(self) -> str:
        return await self._do_request(RPCMethod.web3_clientVersion)

    @property
    async def accounts(self):
        return await self._do_request(RPCMethod.eth_accounts)

    @property
    async def block_number(self) -> int:
        hex_block = await self._do_request(RPCMethod.eth_blockNumber)
        return int(hex_block, 16)

    @property
    async def gas_price(self) -> Wei:
        return Wei(await self._do_request(RPCMethod.eth_gasPrice))

    async def get_balance(self, address: Address) -> Wei:
        assert isinstance(address, Address)
        return Wei(await self._do_request(RPCMethod.eth_getBalance, [address]))

    async def get_transaction_count(self, address: Address, block_identifier: Union[int, str, bytes] = None) -> Wei:
        block_identifier = _format_block_identifier(block_identifier)
        return Wei(await self._do_request(RPCMethod.eth_getTransactionCount, [address, block_identifier]))

    async def get_storage_at(
        self, address: Address, storage_position: Union[int, str], block: Any
    ) -> str:
        if isinstance(block, str) and block in ["latest", "earliest", "pending"]:
            block_param = block
        else:
            block_param = Web3.toHex(block)
        return await self._do_request(
            RPCMethod.eth_getStorageAt,
            [address, Web3.toHex(storage_position), block_param],
        )

    async def get_block_by_hash(self, hash_hex: str, with_details: bool = False):
        return await self._do_request(
            RPCMethod.eth_getBlockByHash, [hash_hex, with_details]
        )

    async def get_block_by_number(self, block_number: int, with_details: bool = False):
        return await self._do_request(
            RPCMethod.eth_getBlockByNumber, [Web3.toHex(block_number), with_details]
        )

    async def call(self, call_transaction: Dict, block_identifier: Union[int, str, bytes] = None):
        block_identifier = _format_block_identifier(block_identifier)
        return await self._do_request(RPCMethod.eth_call, [call_transaction, block_identifier])

    async def send_raw_transaction(self, txdata):
        return await self._do_request(RPCMethod.eth_sendRawTransaction, [txdata])

    async def subscribe_block(self) -> Subscription:
        return await self._do_subscribe("newHeads")

    async def subscribe_syncing(self) -> Subscription:
        return await self._do_subscribe("syncing")

    async def subscribe_new_pending_transaction(self) -> Subscription:
        return await self._do_subscribe("newPendingTransactions")

    async def unsubscribe(self, subscription: Subscription):
        assert isinstance(subscription, Subscription)
        response = await self._do_request(RPCMethod.eth_unsubscribe, [subscription.id])
        assert response
        queue = self._subscriptions[subscription.id]
        del self._subscriptions[subscription.id]
        queue.task_done()

    def contract(self, address: Address, abi: List) -> DeployedContract:
        assert isinstance(address, Address)
        return DeployedContract(self, address, abi)

    async def _do_subscribe(self, param: str):
        subscription_id = await self._do_request(RPCMethod.eth_subscribe, [param])
        queue = asyncio.Queue()
        self._subscriptions[subscription_id] = queue
        return Subscription(subscription_id, queue)

    async def _do_request(self, method, params: Any = None):
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

    async def _ws_process(self):
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

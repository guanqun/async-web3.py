This is an opinionated web3 library.

1. async as the first citizen.
2. websocket support as the first citizen.
3. it supports `eth_subscribe()` and `eth_unsubscribe()`.

```python
import asyncio
from async_web3 import AsyncWeb3
from async_web3.transport import WebsocketTransport

async def main():
    w3 = AsyncWeb3(WebsocketTransport("ws://127.0.0.1:8546"))
    await w3.connect()
    block_stream = await w3.subscribe_block()
    async for new_block in block_stream:
        print(f"got new block: {new_block}")

if __name__ == '__main__':
    asyncio.run(main())
```
4. It has no middleware support.


This library tries to simplify the interaction with the *deployed* contracts. If you want to deploy a new smart contract, please checkout the awesome `brownie` tool.

How to Contribute:

1. install `poetry`
2. under this folder, run `poetry install`
3. then run `poetry shell`
4. start the development
5. run `poetry run pytest`
6. send PR

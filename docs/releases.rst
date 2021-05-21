Release Notes
=============

.. towncrier release notes start

v0.1.0 (2021-05-21)v0.1.0 (2021-05-21)
-------------------

Features
~~~~~~~~

- Adapt brownie's contract infrastructure
  It now supports following operations:
   - web3_clientVersion
   - eth_accounts
   - eth_blockNumber
   - eth_gasPrice
   - eth_getBalance
   - eth_getStorageAt
   - eth_getBlockByHash
   - eth_getBlockByNumber
   - eth_call
  One notable feature of this version is the `eth_subscribe()` and `eth_unsubscribe()`. It compiles smoothly with asyncio. (`#1 <https://github.com/guanqun/async-web3.py/issues/1>`__)



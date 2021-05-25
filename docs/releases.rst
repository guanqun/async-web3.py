Release Notes
=============

.. towncrier release notes start

Async_Web3 0.2.2 (2021-05-25)
-----------------------------

Bugfixes
~~~~~~~~

- Fix the sendRawTransaction not serializable error. (`#11 <https://github.com/guanqun/async-web3.py/issues/11>`__)


v0.1.2 (2021-05-21)
-------------------

Bugfixs
~~~~~~~

- Fix a missing parameter for OverloadedMethod
- Bump the pyproject's version to the right one


v0.1.0 (2021-05-21)
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



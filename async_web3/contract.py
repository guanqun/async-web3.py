# modified from brownie's contract.py file, adapted this to async style

import warnings
from typing import Any, Dict, List, Optional, Tuple, Union, Awaitable

import eth_abi
from hexbytes import HexBytes

from brownie.convert.normalize import format_input, format_output
from brownie.convert.utils import (
    build_function_selector,
    build_function_signature,
    get_type_strings,
)
from brownie.exceptions import (
    BrownieEnvironmentWarning,
    ContractNotFound,
    VirtualMachineError,
)
from brownie.typing import AccountsType, TransactionReceiptType

from eth_account import Account
from eth_account.datastructures import SignedTransaction
from .types import Wei, Address

from eth_utils.toolz import (
    assoc,
    assoc_in,
    dissoc,
)

class _ContractBase:

    def __init__(self, abi: List) -> None:
        self.abi = abi

        # self.topics = _get_topics(self.abi) # CHECK
        self.selectors = {
            build_function_selector(i): i["name"] for i in self.abi if i["type"] == "function"
        }

        # this isn't fully accurate because of overloaded methods - will be removed in `v2.0.0`
        self.signatures = {
            i["name"]: build_function_selector(i) for i in self.abi if i["type"] == "function"
        }

    def get_method(self, calldata: str) -> Optional[str]:
        sig = calldata[:10].lower()
        return self.selectors.get(sig)

    def decode_input(self, calldata: Union[str, bytes]) -> Tuple[str, Any]:
        """
        Decode input calldata for this contract.

        Arguments
        ---------
        calldata : str | bytes
            Calldata for a call to this contract

        Returns
        -------
        str
            Signature of the function that was called
        Any
            Decoded input arguments
        """
        if not isinstance(calldata, HexBytes):
            calldata = HexBytes(calldata)

        abi = next(
            (
                i
                for i in self.abi
                if i["type"] == "function" and build_function_selector(i) == calldata[:4].hex()
            ),
            None,
        )
        if abi is None:
            raise ValueError("Four byte selector does not match the ABI for this contract")

        function_sig = build_function_signature(abi)

        types_list = get_type_strings(abi["inputs"])
        result = eth_abi.decode_abi(types_list, calldata[4:])
        input_args = format_input(abi, result)

        return function_sig, input_args


class DeployedContract(_ContractBase):
    """Methods for interacting with a deployed contract.

    Each public contract method is available as a ContractCall or ContractTx
    instance, created when this class is instantiated.
    """

    _initialized = False

    def __init__(self, web3: 'AsyncWeb3', address: Address, abi: List) -> None:
        super(DeployedContract, self).__init__(abi)

        self.web3 = web3
        self.address = address

        fn_names = [i["name"] for i in self.abi if i["type"] == "function"]
        for abi in [i for i in self.abi if i["type"] == "function"]:
            name = f"{abi['name']}"

            if fn_names.count(abi["name"]) == 1:
                fn = _get_method_object(self.web3, address, abi)
                self._check_and_set(abi["name"], fn)
                continue

            # special logic to handle function overloading
            if not hasattr(self, abi["name"]):
                overloaded = OverloadedMethod(self.web3, address, name)
                self._check_and_set(abi["name"], overloaded)
            getattr(self, abi["name"])._add_fn(abi)

        self._initialized = True

    def _check_and_set(self, name: str, obj: Any) -> None:
        if name == "balance":
            warnings.warn(
                f"defines a 'balance' function, "
                f".balance' is available as .wei_balance",
                BrownieEnvironmentWarning,
            )
            setattr(self, "wei_balance", self.balance)
        elif hasattr(self, name):
            warnings.warn(
                "Namespace collision between contract function and "
                f"brownie `Contract` class member: '.{name}'\n"
                f"The {name} function will not be available when interacting with contract",
                BrownieEnvironmentWarning,
            )
            return
        setattr(self, name, obj)

    def __hash__(self) -> int:
        return hash(f"{self.address}")

    def __str__(self) -> str:
        return self.address

    def __repr__(self) -> str:
        return f"<Contract '{self.address}'>"

    def __getattribute__(self, name: str) -> Any:
        try:
            return super().__getattribute__(name)
        except AttributeError:
            raise AttributeError(f"Contract object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if self._initialized and hasattr(self, name):
            if isinstance(getattr(self, name), _ContractMethod):
                raise AttributeError(
                    f"{name} is a contract function, it cannot be assigned to"
                )
        super().__setattr__(name, value)

    def get_method_object(self, calldata: str) -> Optional["_ContractMethod"]:
        """
        Given a calldata hex string, returns a `ContractMethod` object.
        """
        sig = calldata[:10].lower()
        if sig not in self.selectors:
            return None
        fn = getattr(self, self.selectors[sig], None)
        if isinstance(fn, OverloadedMethod):
            return next((v for v in fn.methods.values() if v.signature == sig), None)
        return fn

    async def balance(self) -> Wei:
        """Returns the current ether balance of the contract, in wei."""
        balance = await self.web3.get_balance(self.address)
        return Wei(balance)


class OverloadedMethod:
    def __init__(self, web3: 'AsyncWeb3', address: str, name: str):
        self._web3 = web3
        self._address = address
        self._name = name
        self.methods: Dict = {}

    def _add_fn(self, abi: Dict) -> None:
        fn = _get_method_object(self._web3, self._address, abi)
        key = tuple(i["type"].replace("256", "") for i in abi["inputs"])
        self.methods[key] = fn

    def _get_fn_from_args(self, args: Tuple) -> "_ContractMethod":
        input_length = len(args)
        if args and isinstance(args[-1], dict):
            input_length -= 1
        keys = [i for i in self.methods if len(i) == input_length]
        if not keys:
            raise ValueError("No function matching the given number of arguments")
        if len(keys) > 1:
            raise ValueError(
                f"Contract has more than one function '{self._name}' requiring "
                f"{input_length} arguments. You must explicitly declare which function "
                f"you are calling, e.g. {self._name}['{','.join(keys[0])}'](*args)"
            )
        return self.methods[keys[0]]

    def __getitem__(self, key: Union[Tuple, str]) -> "_ContractMethod":
        if isinstance(key, str):
            key = tuple(i.strip() for i in key.split(","))

        key = tuple(i.replace("256", "") for i in key)
        return self.methods[key]

    def __repr__(self) -> str:
        return f"<OverloadedMethod '{self._name}'>"

    def __len__(self) -> int:
        return len(self.methods)

    def __call__(self, *args: Tuple) -> Any:
        fn = self._get_fn_from_args(args)
        return fn(*args)  # type: ignore

    def call(self, *args: Tuple, block_identifier: Union[int, str, bytes] = None) -> Awaitable:
        """
        Call the contract method without broadcasting a transaction.

        The specific function called is chosen based on the number of
        arguments given. If more than one function exists with this number
        of arguments, a `ValueError` is raised.

        Arguments
        ---------
        *args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.
        block_identifier : int | str | bytes, optional
            A block number or hash that the call is executed at. If not given, the
            latest block used. Raises `ValueError` if this value is too far in the
            past and you are not using an archival node.

        Returns
        -------
            Contract method return value(s).
        """
        fn = self._get_fn_from_args(args)
        return fn.call(*args, block_identifier=block_identifier)

    def transact(self, *args: Tuple) -> Awaitable:
        """
        Broadcast a transaction that calls this contract method.

        The specific function called is chosen based on the number of
        arguments given. If more than one function exists with this number
        of arguments, a `ValueError` is raised.

        Arguments
        ---------
        *args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.

        Returns
        -------
        TransactionReceipt
            Object representing the broadcasted transaction.
        """
        fn = self._get_fn_from_args(args)
        return fn.transact(*args)

    def encode_input(self, *args: Tuple) -> Any:
        """
        Generate encoded ABI data to call the method with the given arguments.

        Arguments
        ---------
        *args
            Contract method inputs

        Returns
        -------
        str
            Hexstring of encoded ABI data
        """
        fn = self._get_fn_from_args(args)
        return fn.encode_input(*args)

    def decode_input(self, hexstr: str) -> List:
        """
        Decode input call data for this method.

        Arguments
        ---------
        hexstr : str
            Hexstring of input call data

        Returns
        -------
        Decoded values
        """
        selector = HexBytes(hexstr)[:4].hex()
        fn = next((i for i in self.methods.values() if i == selector), None)
        if fn is None:
            raise ValueError(
                "Data cannot be decoded using any input signatures of functions of this name"
            )
        return fn.decode_input(hexstr)

    def decode_output(self, hexstr: str) -> Tuple:
        """
        Decode hexstring data returned by this method.

        Arguments
        ---------
        hexstr : str
            Hexstring of returned call data

        Returns
        -------
        Decoded values
        """
        for fn in self.methods.values():
            try:
                return fn.decode_output(hexstr)
            except Exception:
                pass
        raise ValueError(
            "Data cannot be decoded using any output signatures of functions of this name"
        )

    def info(self) -> None:
        """
        Display NatSpec documentation for this method.
        """
        fn_sigs = []
        for fn in self.methods.values():
            fn_sigs.append(f"{fn.abi['name']}({_inputs(fn.abi)})")
        for sig in sorted(fn_sigs, key=lambda k: len(k)):
            print(sig)


class _ContractMethod:

    def __init__(
        self,
        web3: 'AsyncWeb3',
        address: Address,
        abi: Dict,
    ) -> None:
        self.web3 = web3
        self._address = address
        self.abi = abi
        self.signature = build_function_selector(abi)
        self._input_sig = build_function_signature(abi)

    def __repr__(self) -> str:
        pay = "payable " if self.payable else ""
        return f"<{type(self).__name__} {pay}'{self.abi['name']}({_inputs(self.abi)})'>"

    @property
    def payable(self) -> bool:
        if "payable" in self.abi:
            return self.abi["payable"]
        else:
            return self.abi["stateMutability"] == "payable"

    def info(self) -> None:
        """
        Display NatSpec documentation for this method.
        """
        print(f"{self.abi['name']}({_inputs(self.abi)})")

    async def call(self, *args: Tuple, block_identifier: Union[int, str, bytes] = None) -> Any:
        """
        Call the contract method without broadcasting a transaction.

        Arguments
        ---------
        *args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.
        block_identifier : int | str | bytes, optional
            A block number or hash that the call is executed at. If not given, the
            latest block used. Raises `ValueError` if this value is too far in the
            past and you are not using an archival node.

        Returns
        -------
            Contract method return value(s).
        """

        args, tx = _get_tx(args)
        if tx["from"]:
            tx["from"] = str(tx["from"])
        tx.update({"to": self._address, "data": self.encode_input(*args)})

        data = await self.web3.call({k: v for k, v in tx.items() if v}, block_identifier)

        if HexBytes(data)[:4].hex() == "0x08c379a0":
            revert_str = eth_abi.decode_abi(["string"], HexBytes(data)[4:])[0]
            raise ValueError(f"Call reverted: {revert_str}")
        if self.abi["outputs"] and not data:
            raise ValueError("No data was returned - the call likely reverted")

        return self.decode_output(data)

    def build_transaction(self, *args: Tuple) -> SignedTransaction:
        args, tx = _get_tx(args)

        if not tx["from"]:
            raise AttributeError(
                "Final argument must be a dict of transaction parameters that "
                "includes a `from` field specifying the sender of the transaction"
            )

        if "chainId" not in tx:
            # defaults to chainId = 1
            tx.update({"chainId": 1})
        if "gas" not in tx:
            raise AttributeError("we need 'gas' parameter.")
        if "gasPrice" not in tx:
            raise AttributeError("we need 'gasPrice' parameter.")
        if "nonce" not in tx:
            raise AttributeError("we need 'nonce' parameter.")

        tx.update({"to": self._address, "data": self.encode_input(*args)})

        account = tx["from"]
        signed_txn = account.sign_transaction(dissoc(tx, 'from'))
        return signed_txn

    async def transact(self, *args: Tuple) -> Any:
        """
        Broadcast a transaction that calls this contract method.

        Arguments
        ---------
        *args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.

        Returns
        -------
        TransactionReceipt
            Object representing the broadcasted transaction.
        """
        signed_txn = self.build_transaction(*args)
        return await self.web3.send_raw_transaction(signed_txn.rawTransaction.hex())

    def decode_input(self, hexstr: str) -> List:
        """
        Decode input call data for this method.

        Arguments
        ---------
        hexstr : str
            Hexstring of input call data

        Returns
        -------
        Decoded values
        """
        types_list = get_type_strings(self.abi["inputs"])
        result = eth_abi.decode_abi(types_list, HexBytes(hexstr)[4:])
        return format_input(self.abi, result)

    def encode_input(self, *args: Tuple) -> str:
        """
        Generate encoded ABI data to call the method with the given arguments.

        Arguments
        ---------
        *args
            Contract method inputs

        Returns
        -------
        str
            Hexstring of encoded ABI data
        """
        data = format_input(self.abi, args)
        types_list = get_type_strings(self.abi["inputs"])
        return self.signature + eth_abi.encode_abi(types_list, data).hex()

    def decode_output(self, hexstr: str) -> Tuple:
        """
        Decode hexstring data returned by this method.

        Arguments
        ---------
        hexstr : str
            Hexstring of returned call data

        Returns
        -------
        Decoded values
        """
        types_list = get_type_strings(self.abi["outputs"])
        result = eth_abi.decode_abi(types_list, HexBytes(hexstr))
        result = format_output(self.abi, result)
        if len(result) == 1:
            result = result[0]
        return result

    def estimate_gas(self, *args: Tuple) -> int:
        """
        Estimate the gas cost for a transaction.

        Raises VirtualMachineError if the transaction would revert.

        Arguments
        ---------
        *args
            Contract method inputs

        Returns
        -------
        int
            Estimated gas value in wei.
        """
        args, tx = _get_tx(args)
        if not tx["from"]:
            raise AttributeError(
                "Final argument must be a dict of transaction parameters that "
                "includes a `from` field specifying the sender of the transaction"
            )

        return tx["from"].estimate_gas(
            to=self._address,
            amount=tx["value"],
            gas_price=tx["gasPrice"],
            data=self.encode_input(*args),
        )


class ContractTx(_ContractMethod):
    """
    A public payable or non-payable contract method.

    Attributes
    ----------
    abi : dict
        Contract ABI specific to this method.
    signature : str
        Bytes4 method signature.
    """

    def __call__(self, *args: Tuple) -> Awaitable:
        """
        Broadcast a transaction that calls this contract method.

        Arguments
        ---------
        *args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.

        Returns
        -------
        An Awaitable Object
            Object representing the broadcasted transaction.
        """
        return self.transact(*args)

    def build(self, *args: Tuple) -> SignedTransaction:
        return self.build_transaction(*arg)

class ContractCall(_ContractMethod):

    """
    A public view or pure contract method.

    Attributes
    ----------
    abi : dict
        Contract ABI specific to this method.
    signature : str
        Bytes4 method signature.
    """

    def __call__(self, *args: Tuple, block_identifier: Union[int, str, bytes] = None) -> Awaitable:
        """
        Call the contract method without broadcasting a transaction.

        Arguments
        ---------
        args
            Contract method inputs. You can optionally provide a
            dictionary of transaction properties as the last arg.
        block_identifier : int | str | bytes, optional
            A block number or hash that the call is executed at. If not given, the
            latest block used. Raises `ValueError` if this value is too far in the
            past and you are not using an archival node.

        Returns
        -------
            Contract method return value(s).
        """
        return self.call(*args, block_identifier=block_identifier)


def _get_tx(args: Tuple) -> Tuple:
    tx = {
        "from": None,
        "value": 0,
        "gas": None,
        "gasPrice": None,
        "nonce": None,
    }
    if args and isinstance(args[-1], dict):
        tx.update(args[-1])
        args = args[:-1]
        for key, target in [("amount", "value"), ("gas_limit", "gas"), ("gas_price", "gasPrice")]:
            if key in tx:
                tx[target] = tx[key]

    return args, tx


def _get_method_object(
    web3: 'AsyncWeb3', address: Address, abi: Dict
) -> Union["ContractCall", "ContractTx"]:

    if "constant" in abi:
        constant = abi["constant"]
    else:
        constant = abi["stateMutability"] in ("view", "pure")

    if constant:
        return ContractCall(web3, address, abi)
    return ContractTx(web3, address, abi)


def _inputs(abi: Dict) -> str:
    types_list = get_type_strings(abi["inputs"], {"fixed168x10": "decimal"})
    params = zip([i["name"] for i in abi["inputs"]], types_list)
    return ", ".join(
        f"{i[1]}{' '+i[0] if i[0] else ''}" for i in params
    )

# modified from brownie's contract.py file, adapted this to async style

import json
import os
import re
import time
import warnings
from collections import defaultdict
from pathlib import Path
from textwrap import TextWrapper
from typing import Any, Dict, Iterator, List, Match, Optional, Set, Tuple, Union, Sequence
from urllib.parse import urlparse

import eth_abi
import requests
import solcast
import solcx
from eth_utils import remove_0x_prefix
from hexbytes import HexBytes

from brownie.convert.datatypes import Wei, EthAddress
from brownie.convert.normalize import format_input, format_output
from brownie.convert.utils import (
    build_function_selector,
    build_function_signature,
    get_type_strings,
)
from brownie.exceptions import (
    BrownieCompilerWarning,
    BrownieEnvironmentWarning,
    ContractExists,
    ContractNotFound,
    UndeployedLibrary,
    VirtualMachineError,
)
from brownie.typing import AccountsType, TransactionReceiptType
from brownie.utils.toposort import toposort_flatten


class _ContractBase:

    def __init__(self, address: EthAddress, abi: List) -> None:
        self._address = address
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


class _DeployedContractBase(_ContractBase):
    """Methods for interacting with a deployed contract.

    Each public contract method is available as a ContractCall or ContractTx
    instance, created when this class is instantiated.
    """

    def __init__(
        self, address: EthAddress, owner: Optional[AccountsType] = None, tx: TransactionReceiptType = None
    ) -> None:
        # TODO: what's the purpose of bytecode?
        # self.bytecode = web3.eth.get_code(address).hex()[2:]

        self._owner = owner
        self.tx = tx
        self.address = address

        fn_names = [i["name"] for i in self.abi if i["type"] == "function"]
        for abi in [i for i in self.abi if i["type"] == "function"]:
            name = f"{self._name}.{abi['name']}"
            sig = build_function_signature(abi)

            if fn_names.count(abi["name"]) == 1:
                fn = _get_method_object(address, abi, name, owner)
                self._check_and_set(abi["name"], fn)
                continue

            # special logic to handle function overloading
            if not hasattr(self, abi["name"]):
                overloaded = OverloadedMethod(address, name, owner)
                self._check_and_set(abi["name"], overloaded)
            getattr(self, abi["name"])._add_fn(abi)

    def _check_and_set(self, name: str, obj: Any) -> None:
        if name == "balance":
            warnings.warn(
                f"'{self._name}' defines a 'balance' function, "
                f"'{self._name}.balance' is available as {self._name}.wei_balance",
                BrownieEnvironmentWarning,
            )
            setattr(self, "wei_balance", self.balance)
        elif hasattr(self, name):
            warnings.warn(
                "Namespace collision between contract function and "
                f"brownie `Contract` class member: '{self._name}.{name}'\n"
                f"The {name} function will not be available when interacting with {self._name}",
                BrownieEnvironmentWarning,
            )
            return
        setattr(self, name, obj)

    def __hash__(self) -> int:
        return hash(f"{self._name}{self.address}{self._project}")

    def __str__(self) -> str:
        return self.address

    def __repr__(self) -> str:
        alias = self._build.get("alias")
        if alias:
            return f"<'{alias}' Contract '{self.address}'>"
        return f"<{self._name} Contract '{self.address}'>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _DeployedContractBase):
            return self.address == other.address and self.bytecode == other.bytecode
        if isinstance(other, str):
            try:
                address = _resolve_address(other)
                return address == self.address
            except ValueError:
                return False
        return super().__eq__(other)

    def __getattribute__(self, name: str) -> Any:
        if super().__getattribute__("_reverted"):
            raise ContractNotFound("This contract no longer exists.")
        try:
            return super().__getattribute__(name)
        except AttributeError:
            raise AttributeError(f"Contract '{self._name}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if self._initialized and hasattr(self, name):
            if isinstance(getattr(self, name), _ContractMethod):
                raise AttributeError(
                    f"{self._name}.{name} is a contract function, it cannot be assigned to"
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

    def balance(self) -> Wei:
        """Returns the current ether balance of the contract, in wei."""
        balance = web3.eth.get_balance(self.address)
        return Wei(balance)

    def _deployment_path(self) -> Optional[Path]:
        if not self._project._path or (
            CONFIG.network_type != "live" and not CONFIG.settings["dev_deployment_artifacts"]
        ):
            return None

        chainid = CONFIG.active_network["chainid"] if CONFIG.network_type == "live" else "dev"
        path = self._project._build_path.joinpath(f"deployments/{chainid}")
        path.mkdir(exist_ok=True)
        return path.joinpath(f"{self.address}.json")

    def _save_deployment(self) -> None:
        path = self._deployment_path()
        chainid = CONFIG.active_network["chainid"] if CONFIG.network_type == "live" else "dev"
        deployment_build = self._build.copy()

        deployment_build["deployment"] = {
            "address": self.address,
            "chainid": chainid,
            "blockHeight": web3.eth.block_number,
        }
        if path:
            self._project._add_to_deployment_map(self)
            if not path.exists():
                with path.open("w") as fp:
                    json.dump(deployment_build, fp)

    def _delete_deployment(self) -> None:
        path = self._deployment_path()
        if path:
            self._project._remove_from_deployment_map(self)
            if path.exists():
                path.unlink()


class Contract(_DeployedContractBase):
    """
    Object to interact with a deployed contract outside of a project.
    """

    def __init__(
        self, address_or_alias: str, *args: Any, owner: Optional[AccountsType] = None, **kwargs: Any
    ) -> None:
        """
        Recreate a `Contract` object from the local database.

        The init method is used to access deployments that have already previously
        been stored locally. For new deployments use `from_abi`, `from_ethpm` or
        `from_etherscan`.

        Arguments
        ---------
        address_or_alias : str
            Address or user-defined alias of the deployment.
        owner : Account, optional
            Contract owner. If set, transactions without a `from` field
            will be performed using this account.
        """
        address_or_alias = address_or_alias.strip()

        if args or kwargs:
            warnings.warn(
                "Initializing `Contract` in this manner is deprecated."
                " Use `from_abi` or `from_ethpm` instead.",
                DeprecationWarning,
            )
            kwargs["owner"] = owner
            return self._deprecated_init(address_or_alias, *args, **kwargs)

        address = ""
        try:
            address = _resolve_address(address_or_alias)
            build, sources = _get_deployment(address)
        except Exception:
            build, sources = _get_deployment(alias=address_or_alias)
            if build is not None:
                address = build["address"]

        if build is None or sources is None:
            if (
                not address
                or not CONFIG.settings.get("autofetch_sources")
                or not CONFIG.active_network.get("explorer")
            ):
                if not address:
                    raise ValueError(f"Unknown alias: '{address_or_alias}'")
                else:
                    raise ValueError(f"Unknown contract address: '{address}'")
            contract = self.from_explorer(address, owner=owner, silent=True)
            build, sources = contract._build, contract._sources
            address = contract.address

        _ContractBase.__init__(self, None, build, sources)
        _DeployedContractBase.__init__(self, address, owner)

    def _deprecated_init(
        self,
        name: str,
        address: Optional[str] = None,
        abi: Optional[List] = None,
        manifest_uri: Optional[str] = None,
        owner: Optional[AccountsType] = None,
    ) -> None:
        if manifest_uri and abi:
            raise ValueError("Contract requires either abi or manifest_uri, but not both")
        if manifest_uri is not None:
            manifest = ethpm.get_manifest(manifest_uri)
            abi = manifest["contract_types"][name]["abi"]
            if address is None:
                address_list = ethpm.get_deployment_addresses(manifest, name)
                if not address_list:
                    raise ContractNotFound(
                        f"'{manifest['package_name']}' manifest does not contain"
                        f" a deployment of '{name}' on this chain"
                    )
                if len(address_list) > 1:
                    raise ValueError(
                        f"'{manifest['package_name']}' manifest contains more than one "
                        f"deployment of '{name}' on this chain, you must specify an address:"
                        f" {', '.join(address_list)}"
                    )
                address = address_list[0]
            name = manifest["contract_types"][name]["contract_name"]
        elif not address:
            raise TypeError("Address cannot be None unless creating object from manifest")

        build = {"abi": abi, "contractName": name, "type": "contract"}
        _ContractBase.__init__(self, None, build, {})  # type: ignore
        _DeployedContractBase.__init__(self, address, owner, None)

    @classmethod
    def from_abi(
        cls, name: str, address: str, abi: List, owner: Optional[AccountsType] = None
    ) -> "Contract":
        """
        Create a new `Contract` object from an ABI.

        Arguments
        ---------
        name : str
            Name of the contract.
        address : str
            Address where the contract is deployed.
        abi : dict
            Contract ABI, given as a dictionary.
        owner : Account, optional
            Contract owner. If set, transactions without a `from` field
            will be performed using this account.
        """
        address = _resolve_address(address)
        build = {"abi": abi, "address": address, "contractName": name, "type": "contract"}

        self = cls.__new__(cls)
        _ContractBase.__init__(self, None, build, {})  # type: ignore
        _DeployedContractBase.__init__(self, address, owner, None)
        _add_deployment(self)
        return self

    @classmethod
    def from_ethpm(
        cls,
        name: str,
        manifest_uri: str,
        address: Optional[str] = None,
        owner: Optional[AccountsType] = None,
    ) -> "Contract":
        """
        Create a new `Contract` object from an ethPM manifest.

        Arguments
        ---------
        name : str
            Name of the contract.
        manifest_uri : str
            erc1319 registry URI where the manifest is located
        address : str optional
            Address where the contract is deployed. Only required if the
            manifest contains more than one deployment with the given name
            on the active chain.
        owner : Account, optional
            Contract owner. If set, transactions without a `from` field
            will be performed using this account.
        """
        manifest = ethpm.get_manifest(manifest_uri)

        if address is None:
            address_list = ethpm.get_deployment_addresses(manifest, name)
            if not address_list:
                raise ContractNotFound(
                    f"'{manifest['package_name']}' manifest does not contain"
                    f" a deployment of '{name}' on this chain"
                )
            if len(address_list) > 1:
                raise ValueError(
                    f"'{manifest['package_name']}' manifest contains more than one "
                    f"deployment of '{name}' on this chain, you must specify an address:"
                    f" {', '.join(address_list)}"
                )
            address = address_list[0]

        manifest["contract_types"][name]["contract_name"]
        build = {
            "abi": manifest["contract_types"][name]["abi"],
            "contractName": name,
            "natspec": manifest["contract_types"][name]["natspec"],
            "type": "contract",
        }

        self = cls.__new__(cls)
        _ContractBase.__init__(self, None, build, manifest["sources"])  # type: ignore
        _DeployedContractBase.__init__(self, address, owner)
        _add_deployment(self)
        return self

    @classmethod
    def from_explorer(
        cls,
        address: str,
        as_proxy_for: Optional[str] = None,
        owner: Optional[AccountsType] = None,
        silent: bool = False,
    ) -> "Contract":
        """
        Create a new `Contract` object with source code queried from a block explorer.

        Arguments
        ---------
        address : str
            Address where the contract is deployed.
        as_proxy_for : str, optional
            Address of the implementation contract, if `address` is a proxy contract.
            The generated object will send transactions to `address`, but use the ABI
            and NatSpec of `as_proxy_for`. This field is only required when the
            block explorer API does not provide an implementation address.
        owner : Account, optional
            Contract owner. If set, transactions without a `from` field will be
            performed using this account.
        """
        address = _resolve_address(address)
        data = _fetch_from_explorer(address, "getsourcecode", silent)
        is_verified = bool(data["result"][0].get("SourceCode"))

        if is_verified:
            abi = json.loads(data["result"][0]["ABI"])
            name = data["result"][0]["ContractName"]
        else:
            # if the source is not available, try to fetch only the ABI
            try:
                data_abi = _fetch_from_explorer(address, "getabi", True)
            except ValueError as exc:
                _unverified_addresses.add(address)
                raise exc
            abi = json.loads(data_abi["result"].strip())
            name = "UnknownContractName"
            warnings.warn(
                f"{address}: Was able to fetch the ABI but not the source code. "
                "Some functionality will not be available.",
                BrownieCompilerWarning,
            )

        if as_proxy_for is None:
            # always check for an EIP1967 proxy - https://eips.ethereum.org/EIPS/eip-1967
            implementation_eip1967 = web3.eth.get_storage_at(
                address, int(web3.keccak(text="eip1967.proxy.implementation").hex(), 16) - 1
            )
            # always check for an EIP1822 proxy - https://eips.ethereum.org/EIPS/eip-1822
            implementation_eip1822 = web3.eth.get_storage_at(address, web3.keccak(text="PROXIABLE"))
            if len(implementation_eip1967) > 0 and int(implementation_eip1967.hex(), 16):
                as_proxy_for = _resolve_address(implementation_eip1967[-20:])
            elif len(implementation_eip1822) > 0 and int(implementation_eip1822.hex(), 16):
                as_proxy_for = _resolve_address(implementation_eip1822[-20:])
            elif data["result"][0].get("Implementation"):
                # for other proxy patterns, we only check if etherscan indicates
                # the contract is a proxy. otherwise we could have a false positive
                # if there is an `implementation` method on a regular contract.
                try:
                    # first try to call `implementation` per EIP897
                    # https://eips.ethereum.org/EIPS/eip-897
                    contract = cls.from_abi(name, address, abi)
                    as_proxy_for = contract.implementation.call()
                except Exception:
                    # if that fails, fall back to the address provided by etherscan
                    as_proxy_for = _resolve_address(data["result"][0]["Implementation"])

        if as_proxy_for == address:
            as_proxy_for = None

        # if this is a proxy, fetch information for the implementation contract
        if as_proxy_for is not None:
            implementation_contract = Contract.from_explorer(as_proxy_for)
            abi = implementation_contract._build["abi"]

        if not is_verified:
            return cls.from_abi(name, address, abi, owner)

        compiler_str = data["result"][0]["CompilerVersion"]
        if compiler_str.startswith("vyper:"):
            try:
                version = to_vyper_version(compiler_str[6:])
                is_compilable = version in get_installable_vyper_versions()
            except Exception:
                is_compilable = False
        else:
            try:
                version = Version(compiler_str.lstrip("v")).truncate()
                is_compilable = (
                    version >= Version("0.4.22")
                    and version
                    in solcx.get_installable_solc_versions() + solcx.get_installed_solc_versions()
                )
            except Exception:
                is_compilable = False

        if not is_compilable:
            if not silent:
                warnings.warn(
                    f"{address}: target compiler '{compiler_str}' cannot be installed or is not "
                    "supported by Brownie. Some debugging functionality will not be available.",
                    BrownieCompilerWarning,
                )
            return cls.from_abi(name, address, abi, owner)

        optimizer = {
            "enabled": bool(int(data["result"][0]["OptimizationUsed"])),
            "runs": int(data["result"][0]["Runs"]),
        }
        evm_version = data["result"][0].get("EVMVersion", "Default")
        if evm_version == "Default":
            evm_version = None

        source_str = "\n".join(data["result"][0]["SourceCode"].splitlines())
        if source_str.startswith("{{"):
            # source was verified using compiler standard JSON
            input_json = json.loads(source_str[1:-1])
            sources = {k: v["content"] for k, v in input_json["sources"].items()}
            evm_version = input_json["settings"].get("evmVersion", evm_version)

            compiler.set_solc_version(str(version))
            input_json.update(
                compiler.generate_input_json(sources, optimizer=optimizer, evm_version=evm_version)
            )
            output_json = compiler.compile_from_input_json(input_json)
            build_json = compiler.generate_build_json(input_json, output_json)
        else:
            if source_str.startswith("{"):
                # source was submitted as multiple files
                sources = {k: v["content"] for k, v in json.loads(source_str).items()}
            else:
                # source was submitted as a single file
                if compiler_str.startswith("vyper"):
                    path_str = f"{name}.vy"
                else:
                    path_str = f"{name}-flattened.sol"
                sources = {path_str: source_str}

            build_json = compiler.compile_and_format(
                sources,
                solc_version=str(version),
                vyper_version=str(version),
                optimizer=optimizer,
                evm_version=evm_version,
            )

        build_json = build_json[name]
        if as_proxy_for is not None:
            build_json.update(abi=abi, natspec=implementation_contract._build.get("natspec"))

        if not _verify_deployed_code(
            address, build_json["deployedBytecode"], build_json["language"]
        ):
            warnings.warn(
                f"{address}: Locally compiled and on-chain bytecode do not match!",
                BrownieCompilerWarning,
            )
            del build_json["pcMap"]

        self = cls.__new__(cls)
        _ContractBase.__init__(self, None, build_json, sources)  # type: ignore
        _DeployedContractBase.__init__(self, address, owner)
        _add_deployment(self)
        return self

    def set_alias(self, alias: Optional[str]) -> None:
        """
        Apply a unique alias this object. The alias can be used to restore the
        object in future sessions.

        Arguments
        ---------
        alias: str | None
            An alias to apply. If `None`, any existing alias is removed.
        """
        if "chainid" not in CONFIG.active_network:
            raise ValueError("Cannot set aliases in a development environment")

        if alias is not None:
            if "." in alias or alias.lower().startswith("0x"):
                raise ValueError("Invalid alias")
            build, _ = _get_deployment(alias=alias)
            if build is not None:
                if build["address"] != self.address:
                    raise ValueError("Alias is already in use on another contract")
                return

        _add_deployment(self, alias)
        self._build["alias"] = alias

    @property
    def alias(self) -> Optional[str]:
        return self._build.get("alias")


class OverloadedMethod:
    def __init__(self, address: str, name: str, owner: Optional[AccountsType]):
        self._address = address
        self._name = name
        self._owner = owner
        self.methods: Dict = {}

    def _add_fn(self, abi: Dict) -> None:
        fn = _get_method_object(self._address, abi, self._name, self._owner)
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

    def call(self, *args: Tuple, block_identifier: Union[int, str, bytes] = None) -> Any:
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

    def transact(self, *args: Tuple) -> TransactionReceiptType:
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
        address: str,
        abi: Dict,
        name: str,
    ) -> None:
        self.web3 = web3
        self._address = address
        self._name = name
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

    async def transact(self, *args: Tuple) -> TransactionReceiptType:
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

        args, tx = _get_tx(args)
        if not tx["from"]:
            raise AttributeError(
                "Final argument must be a dict of transaction parameters that "
                "includes a `from` field specifying the sender of the transaction"
            )

        return tx["from"].transfer(
            self._address,
            tx["value"],
            gas_limit=tx["gas"],
            gas_buffer=tx["gas_buffer"],
            gas_price=tx["gasPrice"],
            nonce=tx["nonce"],
            required_confs=tx["required_confs"],
            data=self.encode_input(*args),
            allow_revert=tx["allow_revert"],
        )

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
        args, tx = _get_tx(self._owner, args)
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

    def __call__(self, *args: Tuple) -> TransactionReceiptType:
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

        return self.transact(*args)


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

    def __call__(self, *args: Tuple, block_identifier: Union[int, str, bytes] = None) -> Any:
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

        if block_identifier is not None:
            return self.call(*args, block_identifier=block_identifier)

        args, tx = _get_tx(self._owner, args)
        tx.update({"gas_price": 0, "from": self._owner or accounts[0]})
        pc, revert_msg = None, None

        self.transact(*args, tx)

        try:
            return self.call(*args)
        except VirtualMachineError as exc:
            if pc == exc.pc and revert_msg and exc.revert_msg is None:
                # in case we miss a dev revert string
                exc.revert_msg = revert_msg
            raise exc


def _get_tx(args: Tuple) -> Tuple:
    tx = {
        "from": None,
        "value": 0,
        "gas": None,
        "gas_buffer": None,
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
    address: str, abi: Dict, name: str, owner: Optional[AccountsType]
) -> Union["ContractCall", "ContractTx"]:

    if "constant" in abi:
        constant = abi["constant"]
    else:
        constant = abi["stateMutability"] in ("view", "pure")

    if constant:
        return ContractCall(address, abi, name, owner)
    return ContractTx(address, abi, name, owner)


def _inputs(abi: Dict) -> str:
    types_list = get_type_strings(abi["inputs"], {"fixed168x10": "decimal"})
    params = zip([i["name"] for i in abi["inputs"]], types_list)
    return ", ".join(
        f"{i[1]}{' '+i[0] if i[0] else ''}" for i in params
    )

from typing import List, Dict
from web3.types import Address
import humps
from eth_utils import encode_hex, function_abi_to_4byte_selector
import eth_abi

from web3.exceptions import ABIFunctionNotFound

from .utils import build_function_selector, build_function_signature, format_input, get_type_strings


class ContractFunction:
    def __init__(self, web3, address, fn_abi: Dict):
        self.web3 = web3
        self.contract_address = address
        self.fn_abi = fn_abi
        self.fn_selector = build_function_selector(fn_abi)

    def encode_input(self, *args):
        data = format_input(self.fn_abi, args)
        types_list = get_type_strings(self.fn_abi["inputs"])
        return self.fn_selector + eth_abi.encode_abi(types_list, data).hex()

    def __call__(self, *args, **kwargs):

        # prepare the call parameter
        call_transaction = {}
        call_transaction['to'] = self.contract_address
        call_transaction['data'] = self.encode_input(*args)

        print(call_transaction)
        # selector = encode_hex(function_abi_to_4byte_selector(abi))  # type: ignore

        async def async_call_impl(web3):
            #result = await web3.call(call_transaction)
            #return result
            pass

        return async_call_impl(self.web3, self.func_abi, *args, **kwargs)


class Contract:
    def __init__(self, web3, address: Address, abi: List):
        assert isinstance(abi, List)
        self.web3 = web3
        self.address = address
        self.abi = abi

        # inject function calls
        self._functions = {humps.decamelize(abi["name"]): abi for abi in self.abi if abi["type"] == "function"}
        for func_name, fn_abi in self._functions.items():
            setattr(
                self,
                func_name,
                ContractFunction(self.web3, self.address, fn_abi)
            )

    def __getattr__(self, function_name: str) -> ContractFunction:
        if function_name not in self.__dict__["_functions"]:
            tips = f"we use snake_case instead of camelCase, try {humps.decamelize(function_name)}" if humps.is_camelcase(function_name) else "Are you sure you provided the correct contract abi?"
            raise ABIFunctionNotFound(
                f"The function '{function_name}' was not found in this contract's abi. {tips}"
            )
        else:
            return super().__getattribute__(function_name)

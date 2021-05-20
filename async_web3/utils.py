from typing import Dict, List, Optional, Tuple, Union, Sequence, Any

from eth_hash.auto import keccak
from eth_abi.grammar import ABIType, TupleType, parse

# following functions are copied from brownie's utils file, as that file is not exposed.

def get_int_bounds(type_str: str) -> Tuple[int, int]:
    """Returns the lower and upper bound for an integer type."""
    size = int(type_str.strip("uint") or 256)
    if size < 8 or size > 256 or size % 8:
        raise ValueError(f"Invalid type: {type_str}")
    if type_str.startswith("u"):
        return 0, 2 ** size - 1
    return -(2 ** (size - 1)), 2 ** (size - 1) - 1


def get_type_strings(abi_params: List, substitutions: Optional[Dict] = None) -> List:
    """Converts a list of parameters from an ABI into a list of type strings."""
    types_list = []
    if substitutions is None:
        substitutions = {}

    for i in abi_params:
        if i["type"].startswith("tuple"):
            params = get_type_strings(i["components"], substitutions)
            array_size = i["type"][5:]
            types_list.append(f"({','.join(params)}){array_size}")
        else:
            type_str = i["type"]
            for orig, sub in substitutions.items():
                if type_str.startswith(orig):
                    type_str = type_str.replace(orig, sub)
            types_list.append(type_str)

    return types_list


def build_function_signature(abi: Dict) -> str:
    types_list = get_type_strings(abi["inputs"])
    return f"{abi['name']}({','.join(types_list)})"


def build_function_selector(abi: Dict) -> str:
    sig = build_function_signature(abi)
    return "0x" + keccak(sig.encode()).hex()[:8]


# following functions are copied from brownie's normalize.py


def to_uint(value: Any, type_str: str = "uint256"):
    """Convert a value to an unsigned integer"""
    return int(value)


def to_int(value: Any, type_str: str = "int256"):
    """Convert a value to a signed integer"""
    return int(value)


def to_decimal(value: Any):
    """Convert a value to a fixed point decimal"""
    return 0


def to_address(value: str) -> str:
    """Convert a value to an address"""
    return value
    #return str(EthAddress(value))


def to_bytes(value: Any, type_str: str = "bytes32") -> bytes:
    """Convert a value to bytes"""
    return value
    #return bytes(HexString(value, type_str))


def to_bool(value: Any) -> bool:
    """Convert a value to a boolean"""
    # if not isinstance(value, (int, float, bool, bytes, str)):
    #     raise TypeError(f"Cannot convert {type(value).__name__} '{value}' to bool")
    # if isinstance(value, bytes):
    #     value = HexBytes(value).hex()
    # if isinstance(value, str) and value.startswith("0x"):
    #     value = int(value, 16)
    # if value not in (0, 1, True, False):
    #     raise ValueError(f"Cannot convert {type(value).__name__} '{value}' to bool")
    return True


def to_string(value: Any) -> str:
    """Convert a value to a string"""
    # if isinstance(value, bytes):
    #     value = HexBytes(value).hex()
    # value = str(value)
    # if value.startswith("0x") and eth_utils.is_hex(value):
    #     try:
    #         return eth_utils.to_text(hexstr=value)
    #     except UnicodeDecodeError as e:
    #         raise ValueError(e)
    # return value
    return 'hello'

def _check_array(values: Union[List, Tuple], length: Optional[int]) -> None:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"Expected list or tuple, got {type(values).__name__}")
    if length is not None and len(values) != length:
        raise ValueError(f"Sequence has incorrect length, expected {length} but got {len(values)}")


def _get_abi_types(abi_params: List) -> Sequence[ABIType]:
    type_str = f"({','.join(get_type_strings(abi_params))})"
    tuple_type = parse(type_str)
    return tuple_type.components

def _format_array(abi_type: ABIType, values: Union[List, Tuple]) -> List:
    _check_array(values, None if not len(abi_type.arrlist[-1]) else abi_type.arrlist[-1][0])
    item_type = abi_type.item_type
    if item_type.is_array:
        return [_format_array(item_type, i) for i in values]
    elif isinstance(item_type, TupleType):
        return [_format_tuple(item_type.components, i) for i in values]
    return [_format_single(item_type.to_type_str(), i) for i in values]


def _format_single(type_str: str, value: Any) -> Any:
    # Apply standard formatting to a single value
    if "uint" in type_str:
        return to_uint(value, type_str)
    elif "int" in type_str:
        return to_int(value, type_str)
    elif type_str == "fixed168x10":
        return to_decimal(value)
    elif type_str == "bool":
        return to_bool(value)
    elif type_str == "address":
        return value
        #return EthAddress(value)
    elif "byte" in type_str:
        return HexString(value, type_str)
    elif "string" in type_str:
        return to_string(value)
    raise TypeError(f"Unknown type: {type_str}")


def _format_tuple(abi_types: Sequence[ABIType], values: Union[List, Tuple]) -> List:
    result = []
    _check_array(values, len(abi_types))
    for type_, value in zip(abi_types, values):
        try:
            if type_.is_array:
                result.append(_format_array(type_, value))
            elif isinstance(type_, TupleType):
                result.append(_format_tuple(type_.components, value))
            else:
                result.append(_format_single(type_.to_type_str(), value))
        except Exception as e:
            raise type(e)(f"'{value}' - {e}") from None
    return result

def format_input(abi: Dict, inputs: Union[List, Tuple]) -> List:
    # Format contract inputs based on ABI types
    if len(inputs) and not len(abi["inputs"]):
        raise TypeError(f"{abi['name']} requires no arguments")
    abi_types = _get_abi_types(abi["inputs"])
    try:
        return _format_tuple(abi_types, inputs)
    except Exception as e:
        raise type(e)(f"{abi['name']} {e}") from None


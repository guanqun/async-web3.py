import asyncio
import pytest


def test_smoke():
    print("smoke test")


@pytest.mark.asyncio
async def test_example(event_loop):
    await asyncio.sleep(0, loop=event_loop)

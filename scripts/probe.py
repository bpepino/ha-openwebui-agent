"""Probe discovery or explicitly create one native-agent test chat."""

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import sys
import types

import aiohttp

# Load the HA-independent client without importing HA's integration lifecycle.
package = types.ModuleType("owui_probe")
package.__path__ = [
    str(
        Path(__file__).resolve().parents[1] / "custom_components/openwebui_conversation"
    )
]
sys.modules["owui_probe"] = package
OpenWebUIClient = importlib.import_module("owui_probe.client").OpenWebUIClient
ConversationManager = importlib.import_module("owui_probe.state").ConversationManager
OpenWebUIError = importlib.import_module("owui_probe.exceptions").OpenWebUIError


async def main() -> None:
    """Keep credentials in environment variables and never print exceptions raw."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="A model ID printed by the discovery probe")
    parser.add_argument(
        "--prompt", help="Explicitly create a saved chat; tools may act"
    )
    args = parser.parse_args()
    if bool(args.model) != bool(args.prompt):
        parser.error("Supply both --model and --prompt, or neither")
    if not os.environ.get("OWUI_URL") or not os.environ.get("OWUI_KEY"):
        parser.error("Set OWUI_URL and OWUI_KEY in the environment")
    try:
        async with aiohttp.ClientSession() as session:
            client = OpenWebUIClient(
                os.environ["OWUI_URL"], os.environ["OWUI_KEY"], session
            )
            sys.stdout.write(f"Version: {await client.async_get_version()}\n")
            for model in await client.async_get_models():
                sys.stdout.write(
                    f"Model: {model['id']} ({model.get('name', model['id'])})\n"
                )
            for method, label in (
                (client.async_get_tools, "Tool"),
                (client.async_get_terminals, "Terminal"),
            ):
                try:
                    for item in await method():
                        sys.stdout.write(f"{label}: {item.id} ({item.name})\n")
                except OpenWebUIError as err:
                    sys.stdout.write(f"{label} discovery: {err.key}\n")
            if args.prompt:
                manager = ConversationManager(client)
                try:
                    result = await manager.async_process(
                        "probe", args.prompt, {"chat_model": args.model}
                    )
                    sys.stdout.write(result.text + "\n")
                finally:
                    await manager.async_close()
    except OpenWebUIError as err:
        sys.stderr.write(f"Open WebUI probe failed: {err.key}\n")
        raise SystemExit(1) from None
    except ValueError:
        sys.stderr.write("Invalid Open WebUI URL\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0


import asyncio
import logging

from dotenv import load_dotenv
from src.mcp_server.astronomy_shop_mcp_server import AstronomyShopMcp

logging.basicConfig(level=logging.INFO)

load_dotenv()


async def start_servers():
    """Runs the MCP server."""
    tasks = []
    mcp = AstronomyShopMcp()
    mcp_server_task = asyncio.to_thread(mcp.run)
    tasks.append(mcp_server_task)
    logging.info("Starting MCP server on port %s", mcp.port)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(start_servers())
    except KeyboardInterrupt:
        logging.info("Shutting down servers...")
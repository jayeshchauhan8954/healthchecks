"""TCP proxy to Mailpit that waits SMTP_DELAY seconds before connecting.

Simulates a real SMTP provider (TLS + auth round trips) so that a container
kill can land while an alert email is still being sent.
"""

import asyncio
import os

DELAY = float(os.getenv("SMTP_DELAY", "3"))


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    await asyncio.sleep(DELAY)
    upstream_r, upstream_w = await asyncio.open_connection("mailpit", 1025)
    await asyncio.gather(pipe(client_r, upstream_w), pipe(upstream_r, client_w),
                         return_exceptions=True)


async def main() -> None:
    server = await asyncio.start_server(handle, "", 1025)
    async with server:
        await server.serve_forever()


asyncio.run(main())

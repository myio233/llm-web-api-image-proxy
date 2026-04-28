import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import main


logger = logging.getLogger("llm_web_api_patch")
original_lifespan = main.lifespan


@asynccontextmanager
async def patched_lifespan(app):
    async def _background_lifespan() -> None:
        async with original_lifespan(app):
            await asyncio.Event().wait()

    task = asyncio.create_task(_background_lifespan())

    def _consume_result(finished_task: asyncio.Task) -> None:
        try:
            finished_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Background provider startup task exited with %s: %s",
                type(exc).__name__,
                exc,
            )

    task.add_done_callback(_consume_result)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


main.lifespan = patched_lifespan


if __name__ == "__main__":
    main.api()

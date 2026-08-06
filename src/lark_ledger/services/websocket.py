import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from importlib import import_module
from typing import Any

from lark_ledger.config import Settings
from lark_ledger.services.events import EventService

logger = logging.getLogger(__name__)

SdkClientFactory = Callable[[Callable[[Any], None]], Any]


def adapt_message_event(
    data: Any, marshal: Callable[[Any], str] | None = None
) -> tuple[str, dict[str, Any]]:
    """Convert an SDK P2 message event into the existing processor event shape."""
    if isinstance(data, dict):
        payload = data
    else:
        if marshal is None:
            lark = import_module("lark_oapi")
            marshal = lark.JSON.marshal
        decoded = json.loads(marshal(data))
        if not isinstance(decoded, dict):
            raise ValueError("Feishu SDK event is not an object")
        payload = decoded

    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise ValueError("Feishu SDK event is missing header or event")
    if header.get("event_type") != "im.message.receive_v1":
        raise ValueError("unexpected Feishu event type")
    event_id = str(header.get("event_id") or "")
    if not event_id:
        raise ValueError("Feishu SDK event is missing event_id")
    return event_id, event


class LongConnectionReceiver:
    """Run lark-oapi's reconnecting WebSocket client outside the ASGI event loop."""

    def __init__(
        self,
        settings: Settings,
        event_service: EventService,
        *,
        client_factory: SdkClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._event_service = event_service
        self._client_factory = client_factory or self._build_sdk_client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._processing_tasks: set[asyncio.Task[None]] = set()
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._started = threading.Event()
        self._client: Any = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._status = "stopped"
        self._startup_error: str | None = None
        self._started_once = False
        self._consumer_task_done = False
        self._consumer_task_exception_code: str | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def startup_error(self) -> str | None:
        return self._startup_error

    def health_snapshot(self) -> dict[str, bool | str | None]:
        """Return receiver state without exposing SDK errors or credentials."""
        task = self._consumer_task
        running = bool(
            task is not None
            and not task.done()
            and self._status not in {"stopped", "stopping", "error"}
            and not self._stop_requested.is_set()
        )
        return {
            "started": self._started_once,
            "running": running,
            "stopping": self._status == "stopping" or self._stop_requested.is_set(),
            "task_done": self._consumer_task_done,
            "task_exception": self._consumer_task_exception_code is not None
            or self._status == "error",
            "connection_status": self._status,
            "last_error_code": self._consumer_task_exception_code,
        }

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._consumer_task = asyncio.create_task(self._consume(), name="feishu-event-consumer")
        self._started_once = True
        self._consumer_task_done = False
        self._consumer_task_exception_code = None
        self._consumer_task.add_done_callback(self._consume_task_result)
        self._stop_requested.clear()
        self._started.clear()
        self._startup_error = None
        self._status = "connecting"
        self._thread = threading.Thread(
            target=self._run_client,
            name="feishu-websocket",
            daemon=True,
        )
        self._thread.start()
        await asyncio.to_thread(self._started.wait, 0.25)
        if self._startup_error is not None:
            await self.stop()
            raise RuntimeError(f"failed to start Feishu long connection: {self._startup_error}")

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        self._consumer_task_done = True
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._consumer_task_exception_code = type(exc).__name__
            self._status = "error"
            logger.error(
                "Feishu event consumer exited unexpectedly error_code=%s",
                self._consumer_task_exception_code,
            )

    async def stop(self) -> None:
        self._status = "stopping"
        self._stop_requested.set()
        loop = self._sdk_loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: None)
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 10)
            if thread.is_alive():
                logger.warning("Feishu long connection thread did not stop within 10 seconds")
        task = self._consumer_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._processing_tasks:
            await asyncio.gather(*self._processing_tasks, return_exceptions=True)
        self._thread = None
        self._consumer_task = None
        self._queue = None
        self._status = "stopped"

    def _build_sdk_client(self, callback: Callable[[Any], None]) -> Any:
        lark = import_module("lark_oapi")
        if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
            # lark-oapi 1.7.1 disables websockets 15's environment proxy
            # discovery. Restore the library default only when a proxy is
            # explicitly configured; direct Docker connections are unchanged.
            sdk_ws_client = import_module("lark_oapi.ws.client")
            sdk_ws_client.__dict__["_ws_connect_kwargs"] = lambda: {}
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(callback)
            .build()
        )
        client = lark.ws.Client(
            self._settings.lark_app_id,
            self._settings.lark_app_secret,
            event_handler=event_handler,
            # The SDK's INFO log includes its full temporary connection URL.
            log_level=lark.LogLevel.WARNING,
        )
        client.on_reconnecting = self._on_reconnecting
        client.on_reconnected = self._on_reconnected
        return client

    def _on_reconnecting(self) -> None:
        self._status = "reconnecting"
        logger.warning("Feishu long connection lost; reconnecting")

    def _on_reconnected(self) -> None:
        self._status = "connected"
        logger.info("Feishu long connection re-established")

    def _run_client(self) -> None:
        try:
            asyncio.run(self._run_client_async())
        except Exception as exc:
            self._startup_error = str(exc)
            self._status = "error"
            logger.exception("Feishu long connection stopped unexpectedly")
        finally:
            self._started.set()
            self._sdk_loop = None
            self._client = None

    async def _run_client_async(self) -> None:
        # Construct the SDK client inside its running loop. Its cache schedules
        # a maintenance task during construction.
        client = self._client_factory(self._on_sdk_event)
        self._client = client
        await self._client_lifecycle(client)

    async def _client_lifecycle(self, client: Any) -> None:
        self._sdk_loop = asyncio.get_running_loop()
        while not self._stop_requested.is_set():
            try:
                await client._connect()
                break
            except Exception:
                self._status = "reconnecting"
                logger.warning("Feishu long connection attempt failed; retrying")
                await asyncio.to_thread(self._stop_requested.wait, 5)
        if self._stop_requested.is_set():
            return
        ping_task = asyncio.create_task(client._ping_loop())
        self._status = "connected"
        logger.info("Feishu long connection established")
        self._started.set()
        try:
            await asyncio.to_thread(self._stop_requested.wait)
        finally:
            client._auto_reconnect = False
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)
            receive_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and getattr(task.get_coro(), "__name__", "") == "_receive_message_loop"
            ]
            for receive_task in receive_tasks:
                receive_task.cancel()
            await asyncio.gather(*receive_tasks, return_exceptions=True)
            await client._disconnect()

    def _on_sdk_event(self, data: Any) -> None:
        try:
            item = adapt_message_event(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.exception("failed to adapt Feishu long-connection event")
            return
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._enqueue, item)

    def _enqueue(self, item: tuple[str, dict[str, Any]]) -> None:
        if self._queue is not None:
            self._queue.put_nowait(item)

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            event_id, event = await self._queue.get()
            task = asyncio.create_task(
                self._event_service.handle_safely(
                    event_id,
                    event,
                    transport="websocket",
                ),
                name=f"feishu-event-{event_id}",
            )
            self._processing_tasks.add(task)
            task.add_done_callback(self._processing_tasks.discard)

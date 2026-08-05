import asyncio
import io
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import orjson
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.utils.rotate_1psidts import save_cookies
from loguru import logger

from app.models import AppMessage
from app.utils import g_config
from app.utils.helper import (
    add_tag,
    save_file_to_tempfile,
    save_url_to_tempfile,
)


class GeminiClientWrapper(GeminiClient):
    """Gemini client with helper methods."""

    def __init__(self, client_id: str, **kwargs):
        self._cfg_impersonate: str | None = kwargs.pop("impersonate", None)
        super().__init__(**kwargs)
        self.id = client_id
        self._initialized = False
        self._needs_restart = False
        self._active_requests = 0
        self._active_requests_changed = asyncio.Condition()

    async def init(self, *args: Any, **kwargs: Any) -> None:
        """
        Inject default configuration values from global settings.
        """
        config = g_config.gemini
        init_kwargs: dict[str, Any] = {
            "timeout": config.timeout,
            "watchdog_timeout": config.watchdog_timeout,
            "auto_refresh": config.auto_refresh,
            "refresh_interval": config.refresh_interval,
            "auto_close": config.auto_close,
            "close_delay": config.close_delay,
            "verbose": config.verbose,
        }
        if self._cfg_impersonate is not None:
            init_kwargs["impersonate"] = self._cfg_impersonate
        try:
            await super().init(**init_kwargs)
            self._initialized = True
            self._needs_restart = False
        except Exception:
            self._initialized = False
            self._needs_restart = True
            logger.exception(f"Failed to initialize GeminiClient {self.id}")
            raise

    def running(self) -> bool:
        return self._running

    @property
    def active_requests(self) -> int:
        return self._active_requests

    @property
    def curl_cffi_fetch_options(self) -> dict[str, str | None]:
        """Proxy and TLS fingerprint to reuse when fetching a request's remote media.

        Without this the media fetch leaves from the server's own address with a
        generic fingerprint, while the chat traffic for the same account goes
        through its configured SOCKS proxy -- two different identities for one
        conversation. The configured value is checked first because the parent
        sets self.impersonate to "chrome" in __init__, before init() overwrites
        it, so it is truthy even when a per-client fingerprint is configured.
        """
        return {
            "proxy": self.proxy,
            "impersonate": self._cfg_impersonate or self.impersonate or "chrome",
        }

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator[None]:
        async with self._active_requests_changed:
            self._active_requests += 1
            if self.close_task and not self.close_task.done():
                self.close_task.cancel()
                self.close_task = None

        try:
            yield
        finally:
            async with self._active_requests_changed:
                self._active_requests = max(0, self._active_requests - 1)
                self._active_requests_changed.notify_all()

    async def close(self, delay: float = 0) -> None:
        if delay:
            await asyncio.sleep(delay)
            async with self._active_requests_changed:
                while self._active_requests > 0:
                    logger.debug(
                        f"Gemini client {self.id} auto-close is waiting for "
                        f"{self._active_requests} active request(s)."
                    )
                    await self._active_requests_changed.wait()
            logger.debug(
                f"Auto-close option "
                f"[{'enabled' if self.auto_close else 'disabled'}] "
                f"triggered client closing."
            )

        self._running = False
        current_task = asyncio.current_task()

        if self.close_task:
            if self.close_task is not current_task:
                self.close_task.cancel()
            self.close_task = None

        if self.refresh_task:
            if self.refresh_task is not current_task:
                self.refresh_task.cancel()
            self.refresh_task = None

        if self.activity_task:
            if self.activity_task is not current_task:
                self.activity_task.cancel()
            self.activity_task = None

        if self.client:
            self._cookies.update(self.client.cookies)
            try:
                await asyncio.wait_for(self.client.close(), timeout=10)
            except TimeoutError:
                logger.warning(f"Timed out closing Gemini client {self.id}; dropping session.")
            self.client = None

        try:
            await asyncio.to_thread(save_cookies, self._cookies, self.verbose)
        except OSError as e:
            logger.warning(f"Failed to save cookies to cache file: {e}")

    def mark_unavailable(self) -> None:
        self._needs_restart = True

    def is_healthy(self) -> bool:
        """
        Check if the client is healthy.

        A client is healthy if it is active (running or initialized with auto-close)
        and the account status is available.
        """
        is_active = self._running or (self.auto_close and self._initialized)
        return (
            is_active
            and not self._needs_restart
            and self.account_status == AccountStatus.AVAILABLE
        )

    @staticmethod
    async def _process_content_item(
        item: Any,
        role: str,
        tempdir: Path | None,
        *,
        fetch_proxy: str | None = None,
        fetch_impersonate: str | None = None,
    ) -> tuple[str | None, Path | str | None]:
        """
        Process a single content item (text, image_url, file, input_audio).
        Returns a tuple of (text_fragment, file_path).
        """
        if item.type == "text":
            item_text = getattr(item, "text", "") or ""
            if item_text or role == "tool":
                return item_text, None
        elif item.type == "image_url":
            if item_media_url := getattr(item, "url", None):
                started = time.perf_counter()
                path = await save_url_to_tempfile(
                    item_media_url,
                    tempdir,
                    proxy=fetch_proxy,
                    impersonate=fetch_impersonate,
                )
                logger.info(
                    "Processed image_url content item: path={}, bytes={}, elapsed={:.3f}s",
                    path,
                    path.stat().st_size,
                    time.perf_counter() - started,
                )
                return None, path
            raise ValueError(f"{item.type} cannot be empty")
        elif item.type == "file":
            if not (file_data := getattr(item, "file_data", None)):
                raise ValueError("File must contain 'file_data'")
            filename = getattr(item, "filename", "") or ""
            started = time.perf_counter()
            path = await save_file_to_tempfile(file_data, filename, tempdir)
            logger.info(
                "Processed file content item: filename={}, path={}, bytes={}, elapsed={:.3f}s",
                filename or "<none>",
                path,
                path.stat().st_size,
                time.perf_counter() - started,
            )
            return None, path
        elif item.type == "input_audio":
            if file_data := getattr(item, "file_data", None):
                started = time.perf_counter()
                path = await save_file_to_tempfile(file_data, "audio.wav", tempdir)
                logger.info(
                    "Processed input_audio content item: path={}, bytes={}, elapsed={:.3f}s",
                    path,
                    path.stat().st_size,
                    time.perf_counter() - started,
                )
                return None, path
            raise ValueError("input_audio must contain 'file_data' key")
        return None, None

    @staticmethod
    async def _extract_content_and_files(
        message: AppMessage,
        tempdir: Path | None,
        *,
        fetch_proxy: str | None = None,
        fetch_impersonate: str | None = None,
    ) -> tuple[list[str], list[Path | str]]:
        """
        Extract text fragments and files from message content.
        """
        files: list[Path | str] = []
        text_fragments: list[str] = []

        if isinstance(message.content, str):
            if message.content or message.role == "tool":
                text_fragments.append(message.content or "")
        elif isinstance(message.content, list):
            for item in message.content:
                text, file = await GeminiClientWrapper._process_content_item(
                    item,
                    message.role,
                    tempdir,
                    fetch_proxy=fetch_proxy,
                    fetch_impersonate=fetch_impersonate,
                )
                if text is not None:
                    text_fragments.append(text)
                if file is not None:
                    files.append(file)
        elif message.content is None and message.role == "tool":
            text_fragments.append("")
        elif message.content is not None:
            raise ValueError(f"Unsupported message content type: {type(message.content)}")

        return text_fragments, files

    @staticmethod
    def _format_tool_results(
        text_fragments: list[str], tool_name: str | None, wrap_tool: bool
    ) -> list[str]:
        """
        Format tool results into the PascalCase technical protocol blocks.
        """
        tool_name = tool_name or "unknown"
        combined_content = "\n".join(text_fragments).strip()
        res_block = (
            f"[Result:{tool_name}]\n[ToolResult]\n{combined_content}\n[/ToolResult]\n[/Result]"
        )
        return [f"[ToolResults]\n{res_block}\n[/ToolResults]"] if wrap_tool else [res_block]

    @staticmethod
    def _format_tool_calls(message: AppMessage) -> str | None:
        """
        Format tool calls into the PascalCase technical protocol blocks.
        """
        if not message.tool_calls:
            return None

        tool_blocks: list[str] = []
        for call in message.tool_calls:
            params_text = call.function.arguments.strip()
            formatted_params = ""
            if params_text:
                try:
                    parsed_params = orjson.loads(params_text)
                    if isinstance(parsed_params, dict):
                        for k, v in parsed_params.items():
                            val_str = v if isinstance(v, str) else orjson.dumps(v).decode("utf-8")
                            formatted_params += (
                                f"[CallParameter:{k}]\n```\n{val_str}\n```\n[/CallParameter]\n"
                            )
                    else:
                        formatted_params += f"```\n{params_text}\n```\n"
                except orjson.JSONDecodeError:
                    formatted_params += f"```\n{params_text}\n```\n"

            tool_blocks.append(f"[Call:{call.function.name}]\n{formatted_params}[/Call]")

        return "[ToolCalls]\n" + "\n".join(tool_blocks) + "\n[/ToolCalls]" if tool_blocks else None

    @staticmethod
    async def process_message(
        message: AppMessage,
        tempdir: Path | None = None,
        tagged: bool = True,
        wrap_tool: bool = True,
        *,
        fetch_proxy: str | None = None,
        fetch_impersonate: str | None = None,
    ) -> tuple[str, list[Path | str]]:
        """
        Process a Message into Gemini API format using the PascalCase technical protocol.
        Extracts text, handles files, and appends ToolCalls/ToolResults blocks.
        """
        text_fragments, files = await GeminiClientWrapper._extract_content_and_files(
            message,
            tempdir,
            fetch_proxy=fetch_proxy,
            fetch_impersonate=fetch_impersonate,
        )

        if message.role == "tool":
            text_fragments = GeminiClientWrapper._format_tool_results(
                text_fragments, message.name, wrap_tool
            )

        if tool_section := GeminiClientWrapper._format_tool_calls(message):
            text_fragments.append(tool_section)

        model_input = "\n".join(fragment for fragment in text_fragments if fragment is not None)

        if (model_input or message.role == "tool") and tagged:
            model_input = add_tag(message.role, model_input)

        return model_input, files

    @staticmethod
    async def process_conversation(
        messages: list[AppMessage],
        tempdir: Path | None = None,
        *,
        fetch_proxy: str | None = None,
        fetch_impersonate: str | None = None,
    ) -> tuple[str, list[str | Path | bytes | io.BytesIO]]:
        started = time.perf_counter()
        conversation: list[str] = []
        files: list[str | Path | bytes | io.BytesIO] = []

        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.role == "tool":
                tool_blocks: list[str] = []
                while i < len(messages) and messages[i].role == "tool":
                    part, part_files = await GeminiClientWrapper.process_message(
                        messages[i],
                        tempdir,
                        tagged=False,
                        wrap_tool=False,
                        fetch_proxy=fetch_proxy,
                        fetch_impersonate=fetch_impersonate,
                    )
                    tool_blocks.append(part)
                    files.extend(part_files)
                    i += 1

                combined_tool_content = "\n".join(tool_blocks)
                wrapped_content = f"[ToolResults]\n{combined_tool_content}\n[/ToolResults]"
                conversation.append(add_tag("tool", wrapped_content))
            else:
                input_part, files_part = await GeminiClientWrapper.process_message(
                    msg,
                    tempdir,
                    tagged=True,
                    fetch_proxy=fetch_proxy,
                    fetch_impersonate=fetch_impersonate,
                )
                conversation.append(input_part)
                files.extend(files_part)
                i += 1

        conversation.append(add_tag("assistant", "", unclose=True))
        model_input = "\n".join(conversation)
        logger.info(
            "Processed conversation for Gemini: messages={}, input_chars={}, files={}, elapsed={:.3f}s",
            len(messages),
            len(model_input),
            len(files),
            time.perf_counter() - started,
        )
        return model_input, files

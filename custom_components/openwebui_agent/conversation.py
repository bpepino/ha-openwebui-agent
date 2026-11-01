"""Thin Assist adapter; Open WebUI or the gateway owns context and tools.

Gateway turns are streamed: each sentence goes into Home Assistant's chat log
as it arrives, so the Assist pipeline can start speaking before the answer
ends. Whether the satellite listens again is the gateway's decision
(``continue_conversation``); it is never turned on here.

Gateway mode also shows degraded answers: the gateway's own (its assistant
upstream is offline and Home Assistant's agent answered there), and, when the
owner opts in, Home Assistant's agent answering here while the gateway is
unreachable. Both end listening and are exposed as the entity's ``degraded``
attribute, an ``openwebui_agent_degraded`` event and in diagnostics.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Literal

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    AssistantContentDeltaDict,
    ChatLog,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    intent,
    translation,
)
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from markdown_it import MarkdownIt
from mdit_plain.renderer import RendererPlain

from .const import (
    BACKEND_GATEWAY,
    CONF_FALLBACK,
    CONF_LANGUAGE,
    DEFAULT_OPTIONS,
    DEGRADED_GATEWAY,
    DEGRADED_HOME_ASSISTANT,
    DOMAIN,
    EVENT_DEGRADED,
    GATEWAY_DEFAULT_OPTIONS,
    LOGGER,
    entry_backend,
)
from .exceptions import OpenWebUIError
from .gateway import gateway_device_info
from .gateway_client import TurnResult

# Spoken before Home Assistant's answer while the gateway is unreachable; the
# translation key ``gateway_offline`` localizes it.
OFFLINE_KEY = "gateway_offline"
OFFLINE_EN = "Assistant offline, basic commands only."
ATTR_DEGRADED = "degraded"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the stable, upstream-compatible conversation entity."""
    async_add_entities([OpenWebUIAgent(hass, entry)])


class OpenWebUIAgent(conversation.ConversationEntity):
    """Forward text to Open WebUI or the gateway and return only its final answer."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Reuse the entry's client and isolated conversation mappings."""
        self.hass = hass
        self.entry = entry
        # ConversationManager (Open WebUI) or GatewayClient (gateway mode).
        self.manager = hass.data[DOMAIN][entry.entry_id]
        self._gateway = entry_backend(entry.data) == BACKEND_GATEWAY
        self._attr_unique_id = entry.entry_id
        self._fallback = False
        if self._gateway:
            # Named after the device it shares with speech-to-text and text-to-speech.
            self._attr_name = None
            self._attr_device_info = gateway_device_info(entry, self.manager)
            self._attr_extra_state_attributes = {ATTR_DEGRADED: False}
            # Sentences reach the chat log (and early text-to-speech) as they stream.
            self._attr_supports_streaming = True
            self._fallback = {**GATEWAY_DEFAULT_OPTIONS, **entry.options}[CONF_FALLBACK]
        else:
            self._attr_name = entry.title
        self._markdown = MarkdownIt(renderer_cls=RendererPlain)
        self._talk_sessions: set[str] = set()

    def _continue_conversation(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> bool:
        """Direct mode: HA's question detector plus session-scoped conversation mode."""
        phrase = (
            user_input.text.casefold().strip().rstrip(".!?;，。？！").replace("’", "'")
        )
        conversation_id = chat_log.conversation_id
        if phrase in ("end conversation", "stop talking", "that's all", "that is all"):
            self._talk_sessions.discard(conversation_id)
            return False
        if phrase in (
            "let's talk",
            "lets talk",
            "let us talk",
            "start conversation mode",
        ):
            if conversation_id not in self._talk_sessions:
                self._talk_sessions.add(conversation_id)
                with async_get_chat_session(self.hass, conversation_id) as session:
                    session.async_on_cleanup(
                        lambda: self._talk_sessions.discard(conversation_id)
                    )
        return (
            self.entry.options.get("conversation_mode", "questions") == "always"
            or conversation_id in self._talk_sessions
            or chat_log.continue_conversation
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Allow the selected model to handle the user's language."""
        return MATCH_ALL

    def _device_and_area(
        self, device_id: str | None, satellite_id: str | None
    ) -> tuple[str | None, str | None]:
        """Return the satellite's device and its room name (the model reads names)."""
        area_id = None
        if satellite_id and (entity := er.async_get(self.hass).async_get(satellite_id)):
            area_id = entity.area_id
            device_id = device_id or entity.device_id
        if not area_id and device_id:
            if device := dr.async_get(self.hass).async_get(device_id):
                area_id = device.area_id
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return device_id or satellite_id, area.name if area else area_id

    async def _async_gateway_turn(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> tuple[TurnResult, bool]:
        """Pass what HA knows; the gateway resolves identity, profile and chat.

        Each streamed sentence is added to the chat log as it arrives. HA-side
        prompts (``extra_system_prompt``, for example from a satellite's
        ``start_conversation``) are not forwarded: the gateway seeds its own
        question context. Returns the result and whether sentences streamed.
        """
        options = {**GATEWAY_DEFAULT_OPTIONS, **self.entry.options}
        device_id, area = self._device_and_area(
            user_input.device_id, getattr(user_input, "satellite_id", None)
        )
        deltas: asyncio.Queue[str | None] = asyncio.Queue()
        streamed = False

        def on_delta(text: str) -> None:
            nonlocal streamed
            streamed = True
            deltas.put_nowait(text)

        async def stream() -> AsyncGenerator[AssistantContentDeltaDict]:
            yield {"role": "assistant"}
            while (text := await deltas.get()) is not None:
                yield {"content": text}

        turn = asyncio.create_task(
            self.manager.async_turn(
                user_input.text,
                conversation_id=chat_log.conversation_id,
                language=user_input.language or self.entry.data.get(CONF_LANGUAGE),
                device_id=device_id,
                area=area,
                timeout_s=options["completion_timeout"],
                on_delta=on_delta,
            )
        )
        turn.add_done_callback(lambda _: deltas.put_nowait(None))
        try:
            async for _content in chat_log.async_add_delta_content_stream(
                self.entity_id, stream()
            ):
                pass
            return await turn, streamed
        finally:
            if not turn.done():
                turn.cancel()

    async def _async_message(
        self, language: str, key: str, placeholders: dict | None = None
    ) -> str | None:
        """Translate an ``exceptions`` key, falling back to English."""
        key = f"component.{DOMAIN}.exceptions.{key}.message"
        for lang in dict.fromkeys((language, "en")):
            translations = await translation.async_get_translations(
                self.hass, lang, "exceptions", {DOMAIN}
            )
            if key in translations:
                return translations[key].format(**(placeholders or {}))
        return None

    async def _async_error_message(self, language: str, err: OpenWebUIError) -> str:
        """Translate the error key, falling back to English, then the gateway text."""
        message = await self._async_message(
            language, err.key, getattr(err, "translation_placeholders", {})
        )
        return message or err.server_message or f"Open WebUI Agent failed ({err.key})."

    @callback
    def _async_record_degraded(
        self, source: str | None, user_input: conversation.ConversationInput
    ) -> None:
        """Expose whether this answer was degraded, and fire an event when it was."""
        self.manager.last_degraded = source
        self._attr_extra_state_attributes = {ATTR_DEGRADED: source is not None}
        self.async_write_ha_state()
        if source is None:
            return
        self.manager.degraded_answers += 1
        self.hass.bus.async_fire(
            EVENT_DEGRADED,
            {
                "entity_id": self.entity_id,
                "source": source,
                "device_id": user_input.device_id,
                "satellite_id": getattr(user_input, "satellite_id", None),
            },
            context=user_input.context,
        )

    async def _async_fallback(
        self,
        user_input: conversation.ConversationInput,
        chat_log: ChatLog,
        err: OpenWebUIError,
    ) -> conversation.ConversationResult:
        """Let Home Assistant's own agent answer while the gateway is unreachable."""
        LOGGER.warning(
            "AI gateway unreachable (%s); Home Assistant's agent answers", err.key
        )
        try:
            result = await conversation.async_converse(
                self.hass,
                user_input.text,
                chat_log.conversation_id,
                user_input.context,
                language=user_input.language,
                agent_id=conversation.HOME_ASSISTANT_AGENT,
                device_id=user_input.device_id,
                satellite_id=getattr(user_input, "satellite_id", None),
            )
        except Exception:  # noqa: BLE001 - report the gateway failure instead
            LOGGER.exception("Home Assistant's agent failed as the fallback")
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                await self._async_error_message(user_input.language, err),
            )
            self._async_record_degraded(None, user_input)
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )
        response = result.response
        speech = response.speech.get("plain", {}).get("speech", "")
        prefix = await self._async_message(user_input.language, OFFLINE_KEY)
        response.async_set_speech(f"{prefix or OFFLINE_EN} {speech}".strip())
        self._async_record_degraded(DEGRADED_HOME_ASSISTANT, user_input)
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=False,
        )

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> conversation.ConversationResult:
        """Use HA's managed conversation ID without registering any HA LLM API."""
        response = intent.IntentResponse(language=user_input.language)
        continue_conversation = follow_up = degraded = streamed = False
        try:
            if self._gateway:
                result, streamed = await self._async_gateway_turn(user_input, chat_log)
                text, speech = result.display_text, result.speech_text
                # An older gateway leaves continue_conversation out.
                follow_up = (
                    result.expects_reply
                    if result.continue_conversation is None
                    else result.continue_conversation
                )
                # Home Assistant's agent answered at the gateway: never retried.
                degraded = result.degraded
            else:
                result = await self.manager.async_process(
                    chat_log.conversation_id, user_input.text, dict(self.entry.options)
                )
                text = speech = result.text
        except OpenWebUIError as err:
            if self._gateway and self._fallback and err.unreachable:
                return await self._async_fallback(user_input, chat_log, err)
            LOGGER.warning("Open WebUI agent failed: %s", err.key)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                await self._async_error_message(user_input.language, err),
            )
        else:
            # The gateway's display_text is already plain and its speech_text
            # TTS-ready; only Open WebUI's own answer is Markdown.
            if not self._gateway and self.entry.options.get(
                "strip_markdown", DEFAULT_OPTIONS["strip_markdown"]
            ):
                text = speech = self._markdown.render(text).strip()
            if not streamed:
                chat_log.async_add_assistant_content_without_tools(
                    AssistantContent(agent_id=self.entity_id, content=text)
                )
            response.async_set_speech(speech)
            if self._gateway:
                # The gateway decides (its follow-up switch, a question, no
                # pending confirmation). A degraded answer never listens on.
                continue_conversation = follow_up and not degraded
            else:
                continue_conversation = self._continue_conversation(
                    user_input, chat_log
                )
        if self._gateway:
            self._async_record_degraded(
                DEGRADED_GATEWAY if degraded else None, user_input
            )
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=continue_conversation,
        )

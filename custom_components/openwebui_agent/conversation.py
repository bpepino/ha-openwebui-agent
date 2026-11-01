"""Thin Assist adapter; Open WebUI or the gateway owns context and tools."""

from typing import Literal

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import AssistantContent, ChatLog
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
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
    CONF_LANGUAGE,
    DEFAULT_OPTIONS,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
    LOGGER,
    entry_backend,
)
from .exceptions import OpenWebUIError
from .gateway import gateway_device_info


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
        if self._gateway:
            # Named after the device it shares with speech-to-text and text-to-speech.
            self._attr_name = None
            self._attr_device_info = gateway_device_info(entry, self.manager)
        else:
            self._attr_name = entry.title
        self._markdown = MarkdownIt(renderer_cls=RendererPlain)
        self._talk_sessions: set[str] = set()

    def _continue_conversation(
        self,
        user_input: conversation.ConversationInput,
        chat_log: ChatLog,
        expects_reply: bool = False,
    ) -> bool:
        """Use HA's question detector plus session-scoped voice conversation mode."""
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
            or expects_reply
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
    ):
        """Pass what HA knows; the gateway resolves identity, profile and chat."""
        options = {**GATEWAY_DEFAULT_OPTIONS, **self.entry.options}
        device_id, area = self._device_and_area(
            user_input.device_id, getattr(user_input, "satellite_id", None)
        )
        return await self.manager.async_turn(
            user_input.text,
            conversation_id=chat_log.conversation_id,
            language=user_input.language or self.entry.data.get(CONF_LANGUAGE),
            device_id=device_id,
            area=area,
            extra_instructions=getattr(user_input, "extra_system_prompt", None),
            timeout_s=options["completion_timeout"],
        )

    async def _async_error_message(self, language: str, err: OpenWebUIError) -> str:
        """Translate the error key, falling back to English, then the gateway text."""
        key = f"component.{DOMAIN}.exceptions.{err.key}.message"
        for lang in dict.fromkeys((language, "en")):
            translations = await translation.async_get_translations(
                self.hass, lang, "exceptions", {DOMAIN}
            )
            if key in translations:
                return translations[key].format(
                    **getattr(err, "translation_placeholders", {})
                )
        return err.server_message or f"Open WebUI Agent failed ({err.key})."

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> conversation.ConversationResult:
        """Use HA's managed conversation ID without registering any HA LLM API."""
        response = intent.IntentResponse(language=user_input.language)
        continue_conversation = False
        expects_reply = False
        try:
            if self._gateway:
                result = await self._async_gateway_turn(user_input, chat_log)
                text, speech = result.display_text, result.speech_text
                expects_reply = result.expects_reply
            else:
                result = await self.manager.async_process(
                    chat_log.conversation_id, user_input.text, dict(self.entry.options)
                )
                text = speech = result.text
        except OpenWebUIError as err:
            LOGGER.warning("Open WebUI agent failed: %s", err.key)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                await self._async_error_message(user_input.language, err),
            )
        else:
            if self.entry.options.get(
                "strip_markdown", DEFAULT_OPTIONS["strip_markdown"]
            ):
                text = self._markdown.render(text).strip()
                if not self._gateway:
                    speech = text
            chat_log.async_add_assistant_content_without_tools(
                AssistantContent(agent_id=self.entity_id, content=text)
            )
            # Gateway speech_text is already TTS-ready (no markup, links or URLs).
            response.async_set_speech(speech)
            continue_conversation = self._continue_conversation(
                user_input, chat_log, expects_reply
            )
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=continue_conversation,
        )

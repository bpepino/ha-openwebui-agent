"""Thin Assist adapter; Open WebUI owns all model context and tool execution."""

from typing import Literal

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import AssistantContent, ChatLog
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, translation
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from markdown_it import MarkdownIt
from mdit_plain.renderer import RendererPlain

from .const import DEFAULT_OPTIONS, DOMAIN, LOGGER
from .exceptions import OpenWebUIError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the stable, upstream-compatible conversation entity."""
    async_add_entities([OpenWebUIAgent(hass, entry)])


class OpenWebUIAgent(conversation.ConversationEntity):
    """Forward text to Open WebUI and return only its final answer."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Reuse the entry's client and isolated conversation mappings."""
        self.hass = hass
        self.entry = entry
        self.manager = hass.data[DOMAIN][entry.entry_id]
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        self._markdown = MarkdownIt(renderer_cls=RendererPlain)
        self._talk_sessions: set[str] = set()

    def _continue_conversation(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
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
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Allow the selected model to handle the user's language."""
        return MATCH_ALL

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> conversation.ConversationResult:
        """Use HA's managed conversation ID without registering any HA LLM API."""
        response = intent.IntentResponse(language=user_input.language)
        continue_conversation = False
        try:
            result = await self.manager.async_process(
                chat_log.conversation_id, user_input.text, dict(self.entry.options)
            )
        except OpenWebUIError as err:
            LOGGER.warning("Open WebUI agent failed: %s", err.key)
            translations = await translation.async_get_translations(
                self.hass,
                user_input.language,
                "exceptions",
                {DOMAIN},
            )
            key = f"component.{DOMAIN}.exceptions.{err.key}.message"
            if key not in translations:
                translations = await translation.async_get_translations(
                    self.hass, "en", "exceptions", {DOMAIN}
                )
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                translations[key].format(
                    **getattr(err, "translation_placeholders", {})
                ),
            )
        else:
            text = result.text
            if self.entry.options.get(
                "strip_markdown", DEFAULT_OPTIONS["strip_markdown"]
            ):
                text = self._markdown.render(text).strip()
            chat_log.async_add_assistant_content_without_tools(
                AssistantContent(agent_id=self.entity_id, content=text)
            )
            response.async_set_speech(text)
            continue_conversation = self._continue_conversation(user_input, chat_log)
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=continue_conversation,
        )

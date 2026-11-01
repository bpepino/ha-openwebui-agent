"""Text-to-speech through the gateway's OpenAI-compatible speech route.

With **Speak while the answer streams** on (the default), Home Assistant hands
the answer over as it streams; each sentence is spoken (MP3) as soon as it is
complete, and the audio of all sentences is played as one stream. Off, the
whole answer is spoken in one call in the configured format.
"""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LANGUAGE,
    CONF_STREAM_SPEECH,
    CONF_TTS_FORMAT,
    CONF_TTS_VOICE,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
    LANGUAGES,
    LOGGER,
    STREAM_FORMAT,
    TTS_FORMATS,
    default_language,
)
from .exceptions import OpenWebUIError
from .gateway import gateway_device_info
from .sentences import SentenceSplitter

ATTR_RESPONSE_FORMAT = "response_format"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the gateway's text-to-speech entity."""
    async_add_entities([GatewayTextToSpeech(hass, entry)])


class GatewayTextToSpeech(tts.TextToSpeechEntity):
    """The gateway translates if needed, picks the engine and synthesizes."""

    _attr_has_entity_name = True
    _attr_translation_key = "text_to_speech"
    _attr_supported_languages = list(LANGUAGES)
    _attr_supported_options = [tts.ATTR_VOICE, ATTR_RESPONSE_FORMAT]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Share the entry's gateway client and device; options are the defaults."""
        self.client = hass.data[DOMAIN][entry.entry_id]
        options = {**GATEWAY_DEFAULT_OPTIONS, **entry.options}
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_device_info = gateway_device_info(entry, self.client)
        self._attr_default_language = default_language(
            entry.data.get(CONF_LANGUAGE) or hass.config.language
        )
        # Defaults take part in HA's TTS cache key, so a changed voice or format
        # never replays audio cached with the old one.
        self._attr_default_options = {ATTR_RESPONSE_FORMAT: options[CONF_TTS_FORMAT]}
        if options[CONF_TTS_VOICE]:
            self._attr_default_options[tts.ATTR_VOICE] = options[CONF_TTS_VOICE]
        self._stream_speech = options[CONF_STREAM_SPEECH]

    @callback
    def async_supports_streaming_input(self) -> bool:
        """Take streamed text unless the owner turned it off."""
        return self._stream_speech

    async def async_stream_tts_audio(
        self, request: tts.TTSAudioRequest
    ) -> tts.TTSAudioResponse:
        """Speak each sentence as soon as the streamed text completes it."""
        voice = request.options.get(tts.ATTR_VOICE)

        async def sentences() -> AsyncGenerator[str]:
            splitter = SentenceSplitter()
            async for text in request.message_gen:
                for sentence in splitter.feed(text):
                    yield sentence
            for sentence in splitter.flush():
                yield sentence

        async def audio() -> AsyncGenerator[bytes]:
            async with aclosing(sentences()) as texts:
                async for sentence in texts:
                    try:
                        async with aclosing(
                            self.client.async_speech_stream(
                                sentence, voice=voice, response_format=STREAM_FORMAT
                            )
                        ) as chunks:
                            async for chunk in chunks:
                                yield chunk
                    except OpenWebUIError as err:
                        LOGGER.warning("Gateway text-to-speech failed: %s", err.key)
                        raise HomeAssistantError(
                            f"The AI gateway could not synthesize speech ({err.key})"
                        ) from err

        # Per-sentence MP3 files concatenate into one playable MP3 stream.
        return tts.TTSAudioResponse(TTS_FORMATS[STREAM_FORMAT], audio())

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> tts.TtsAudioType:
        """Return the file extension and the gateway's audio."""
        response_format = options.get(ATTR_RESPONSE_FORMAT, "mp3")
        if response_format not in TTS_FORMATS:
            raise HomeAssistantError(f"Unsupported response format {response_format}")
        try:
            audio = await self.client.async_speech(
                message,
                voice=options.get(tts.ATTR_VOICE),
                response_format=response_format,
            )
        except OpenWebUIError as err:
            LOGGER.warning("Gateway text-to-speech failed: %s", err.key)
            raise HomeAssistantError(
                f"The AI gateway could not synthesize speech ({err.key})"
            ) from err
        return TTS_FORMATS[response_format], audio

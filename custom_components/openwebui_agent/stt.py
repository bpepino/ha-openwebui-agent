"""Speech-to-text through the gateway's OpenAI-compatible transcription route."""

from collections.abc import AsyncIterable
import io
import wave

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LANGUAGE, DOMAIN, LANGUAGES, LOGGER
from .exceptions import OpenWebUIError
from .gateway import gateway_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the gateway's speech-to-text entity."""
    async_add_entities([GatewaySpeechToText(hass, entry)])


def pcm_to_wav(pcm: bytes, metadata: stt.SpeechMetadata) -> bytes:
    """Assist streams raw PCM for WAV; the gateway expects a complete file."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(metadata.channel.value)
        wav.setsampwidth(metadata.bit_rate.value // 8)
        wav.setframerate(metadata.sample_rate.value)
        wav.writeframes(pcm)
    return buffer.getvalue()


class GatewaySpeechToText(stt.SpeechToTextEntity):
    """Send the recording to the gateway; it identifies the speaker and repairs the text."""

    _attr_has_entity_name = True
    _attr_translation_key = "speech_to_text"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Share the entry's gateway client and device."""
        self.entry = entry
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = gateway_device_info(entry, self.client)

    @property
    def supported_languages(self) -> list[str]:
        """Languages of the gateway's Whisper-based recognition."""
        return list(LANGUAGES)

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """The gateway normalizes any container with ffmpeg."""
        return [stt.AudioFormats.WAV, stt.AudioFormats.OGG]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Supported audio codecs."""
        return [stt.AudioCodecs.PCM, stt.AudioCodecs.OPUS]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Supported bit rates."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Supported sample rates."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Supported audio channels."""
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Upload the whole utterance in the configured language."""
        audio = b"".join([chunk async for chunk in stream])
        if metadata.codec == stt.AudioCodecs.PCM:
            audio, filename, content_type = (
                pcm_to_wav(audio, metadata),
                "audio.wav",
                "audio/wav",
            )
        else:
            filename, content_type = "audio.ogg", "audio/ogg"
        try:
            text = await self.client.async_transcribe(
                audio,
                language=self.entry.data.get(CONF_LANGUAGE) or metadata.language,
                filename=filename,
                content_type=content_type,
            )
        except OpenWebUIError as err:
            LOGGER.warning("Gateway speech-to-text failed: %s", err.key)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        # Empty text is a success without words; Assist reports "no text recognized".
        return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)

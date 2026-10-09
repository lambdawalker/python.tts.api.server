"""Explicit development adapter. Produces silence, never synthetic speech."""

import io
import wave

from .adapter import Adapter, AudioOutput, ExecutionContext, Profile
from .models import Voice


class FakeAdapter(Adapter):
    def profiles(self):
        return [
            Profile(
                id="fake-v1",
                description="Test adapter: outputs silence, not speech.",
                checkpoint="builtin/fake",
                checkpoint_revision="1",
                adapter_version="0.1.0",
                availability="ready",
                instructions=True,
                languages=["en"],
                features={
                    "tts": True,
                    "voice_cloning": True,
                    "voice_design": True,
                    "voice_conversion": False,
                },
            )
        ]

    def guidance(self, profile, feature):
        return {
            "summary": "Test transport and persistence with a silent WAV.",
            "input_rules": ["This adapter does not synthesize speech."],
            "examples": [{"model": profile, "text": "Hello"}] if feature == "tts" else [],
            "limitations": ["Not an acoustic model or a GPU test."],
        }

    def execute(
        self,
        operation: str,
        profile: str,
        request: dict,
        voice: Voice | None,
        context: ExecutionContext,
    ):
        context.emit("generating", None)
        stream = io.BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\x00\x00" * 2400)
        return [AudioOutput(stream.getvalue(), provenance={"test_audio": True})]

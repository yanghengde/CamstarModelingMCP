"""Provider-neutral streaming metrics; missing usage is unknown, never zero."""
import time
import os


def stream_options():
    # Opt in only for providers supporting OpenAI streaming usage options.
    return {"stream_options": {"include_usage": True}} if os.getenv("LLM_STREAM_USAGE", "false").lower() == "true" else {}


class InferenceMetrics:
    def __init__(self):
        self.started = time.perf_counter()
        self.first_delta_seconds = None
        self.first_event_seconds = None
        self.output_chars = 0
        self.reasoning_chars = 0
        self.usage = None

    def observe(self, chunk):
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = usage.model_dump() if hasattr(usage, "model_dump") else usage if isinstance(usage, dict) else None
        for choice in getattr(chunk, "choices", []):
            delta = choice.delta
            reasoning = getattr(delta, 'reasoning_content', None) or ''
            content = getattr(delta, 'content', None) or ''
            self.reasoning_chars += len(reasoning)
            self.output_chars += len(content)
            if (reasoning or content or getattr(delta, 'tool_calls', None)) and self.first_event_seconds is None:
                self.first_event_seconds = time.perf_counter() - self.started
            if (getattr(delta, "content", None) or getattr(delta, "tool_calls", None)) and self.first_delta_seconds is None:
                self.first_delta_seconds = time.perf_counter() - self.started

    def result(self):
        return {"first_delta_seconds": self.first_delta_seconds, "usage": self.usage,
                "usage_available": self.usage is not None, 'first_event_seconds': self.first_event_seconds,
                'output_chars': self.output_chars, 'reasoning_chars': self.reasoning_chars}

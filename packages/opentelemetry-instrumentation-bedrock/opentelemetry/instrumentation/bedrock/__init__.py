"""OpenTelemetry Bedrock instrumentation"""

from functools import wraps
import json
import logging
import os
from typing import Collection
from opentelemetry.instrumentation.bedrock.config import Config
from opentelemetry.instrumentation.bedrock.reusable_streaming_body import (
    ReusableStreamingBody,
)
from opentelemetry.instrumentation.bedrock.streaming_wrapper import StreamingWrapper
from opentelemetry.instrumentation.bedrock.utils import dont_throw
from wrapt import wrap_function_wrapper
import anthropic

from opentelemetry import context as context_api
from opentelemetry.trace import get_tracer, SpanKind

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    unwrap,
)

from opentelemetry.semconv.ai import (
    SUPPRESS_LANGUAGE_MODEL_INSTRUMENTATION_KEY,
    SpanAttributes,
    LLMRequestTypeValues,
)
from opentelemetry.instrumentation.bedrock.version import __version__

logger = logging.getLogger(__name__)

anthropic_client = anthropic.Anthropic()

_instruments = ("boto3 >= 1.28.57",)

WRAPPED_METHODS = [
    {
        "package": "botocore.client",
        "object": "ClientCreator",
        "method": "create_client",
    },
    {"package": "botocore.session", "object": "Session", "method": "create_client"},
]


def should_send_prompts():
    return (
        os.getenv("TRACELOOP_TRACE_CONTENT") or "true"
    ).lower() == "true" or context_api.get_value("override_enable_content_tracing")


def _set_span_attribute(span, name, value):
    if value is not None:
        if value != "":
            span.set_attribute(name, value)
    return


def _with_tracer_wrapper(func):
    """Helper for providing tracer for wrapper functions."""

    def _with_tracer(tracer, to_wrap):
        def wrapper(wrapped, instance, args, kwargs):
            return func(tracer, to_wrap, wrapped, instance, args, kwargs)

        return wrapper

    return _with_tracer


@_with_tracer_wrapper
def _wrap(tracer, to_wrap, wrapped, instance, args, kwargs):
    """Instruments and calls every function defined in TO_WRAP."""
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    if kwargs.get("service_name") == "bedrock-runtime":
        client = wrapped(*args, **kwargs)
        client.invoke_model = _instrumented_model_invoke(client.invoke_model, tracer)
        client.invoke_model_with_response_stream = (
            _instrumented_model_invoke_with_response_stream(
                client.invoke_model_with_response_stream, tracer
            )
        )

        return client

    return wrapped(*args, **kwargs)


def _instrumented_model_invoke(fn, tracer):
    @wraps(fn)
    def with_instrumentation(*args, **kwargs):
        if context_api.get_value(SUPPRESS_LANGUAGE_MODEL_INSTRUMENTATION_KEY):
            return fn(*args, **kwargs)

        with tracer.start_as_current_span(
            "bedrock.completion", kind=SpanKind.CLIENT
        ) as span:
            response = fn(*args, **kwargs)

            if span.is_recording():
                _handle_call(span, kwargs, response)

            return response

    return with_instrumentation


def _instrumented_model_invoke_with_response_stream(fn, tracer):
    @wraps(fn)
    def with_instrumentation(*args, **kwargs):
        if context_api.get_value(SUPPRESS_LANGUAGE_MODEL_INSTRUMENTATION_KEY):
            return fn(*args, **kwargs)

        span = tracer.start_span("bedrock.completion", kind=SpanKind.CLIENT)
        response = fn(*args, **kwargs)

        if span.is_recording():
            _handle_stream_call(span, kwargs, response)

        return response

    return with_instrumentation


def _handle_stream_call(span, kwargs, response):
    @dont_throw
    def stream_done(response_body):
        request_body = json.loads(kwargs.get("body"))

        (vendor, model) = kwargs.get("modelId").split(".")

        _set_span_attribute(span, SpanAttributes.LLM_SYSTEM, vendor)
        _set_span_attribute(span, SpanAttributes.LLM_REQUEST_MODEL, model)

        if vendor == "cohere":
            _set_cohere_span_attributes(span, request_body, response_body)
        elif vendor == "anthropic":
            if "prompt" in request_body:
                _set_anthropic_completion_span_attributes(
                    span, request_body, response_body
                )
            elif "messages" in request_body:
                _set_anthropic_messages_span_attributes(
                    span, request_body, response_body
                )
        elif vendor == "ai21":
            _set_ai21_span_attributes(span, request_body, response_body)
        elif vendor == "meta":
            _set_llama_span_attributes(span, request_body, response_body)

        span.end()

    response["body"] = StreamingWrapper(response["body"], stream_done)


@dont_throw
def _handle_call(span, kwargs, response):
    response["body"] = ReusableStreamingBody(
        response["body"]._raw_stream, response["body"]._content_length
    )
    request_body = json.loads(kwargs.get("body"))
    response_body = json.loads(response.get("body").read())

    (vendor, model) = kwargs.get("modelId").split(".")

    _set_span_attribute(span, SpanAttributes.LLM_SYSTEM, vendor)
    _set_span_attribute(span, SpanAttributes.LLM_REQUEST_MODEL, model)

    if vendor == "cohere":
        _set_cohere_span_attributes(span, request_body, response_body)
    elif vendor == "anthropic":
        if "prompt" in request_body:
            _set_anthropic_completion_span_attributes(span, request_body, response_body)
        elif "messages" in request_body:
            _set_anthropic_messages_span_attributes(span, request_body, response_body)
    elif vendor == "ai21":
        _set_ai21_span_attributes(span, request_body, response_body)
    elif vendor == "meta":
        _set_llama_span_attributes(span, request_body, response_body)


def _record_usage_to_span(span, prompt_tokens, completion_tokens):
    _set_span_attribute(
        span,
        SpanAttributes.LLM_USAGE_PROMPT_TOKENS,
        prompt_tokens,
    )
    _set_span_attribute(
        span,
        SpanAttributes.LLM_USAGE_COMPLETION_TOKENS,
        completion_tokens,
    )
    _set_span_attribute(
        span,
        SpanAttributes.LLM_USAGE_TOTAL_TOKENS,
        prompt_tokens + completion_tokens,
    )


def _set_cohere_span_attributes(span, request_body, response_body):
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TYPE, LLMRequestTypeValues.COMPLETION.value
    )
    _set_span_attribute(span, SpanAttributes.LLM_REQUEST_TOP_P, request_body.get("p"))
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TEMPERATURE, request_body.get("temperature")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_MAX_TOKENS, request_body.get("max_tokens")
    )

    # based on contract at
    # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-cohere-command-r-plus.html
    _record_usage_to_span(
        span,
        response_body.get("token_count").get("prompt_tokens"),
        response_body.get("token_count").get("response_tokens"),
    )

    if should_send_prompts():
        _set_span_attribute(
            span, f"{SpanAttributes.LLM_PROMPTS}.0.user", request_body.get("prompt")
        )

        for i, generation in enumerate(response_body.get("generations")):
            _set_span_attribute(
                span,
                f"{SpanAttributes.LLM_COMPLETIONS}.{i}.content",
                generation.get("text"),
            )


def _set_anthropic_completion_span_attributes(span, request_body, response_body):
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TYPE, LLMRequestTypeValues.COMPLETION.value
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TOP_P, request_body.get("top_p")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TEMPERATURE, request_body.get("temperature")
    )
    _set_span_attribute(
        span,
        SpanAttributes.LLM_REQUEST_MAX_TOKENS,
        request_body.get("max_tokens_to_sample"),
    )

    if (
        response_body.get("usage") is not None
        and response_body.get("usage").get("input_tokens") is not None
        and response_body.get("usage").get("output_tokens") is not None
    ):
        _record_usage_to_span(
            span,
            response_body.get("usage").get("input_tokens"),
            response_body.get("usage").get("output_tokens"),
        )
    elif response_body.get("invocation_metrics") is not None:
        _record_usage_to_span(
            span,
            response_body.get("invocation_metrics").get("inputTokenCount"),
            response_body.get("invocation_metrics").get("outputTokenCount"),
        )
    elif Config.enrich_token_usage:
        _record_usage_to_span(
            span,
            _count_anthropic_tokens([request_body.get("prompt")]),
            _count_anthropic_tokens([response_body.get("completion")]),
        )

    if should_send_prompts():
        _set_span_attribute(
            span, f"{SpanAttributes.LLM_PROMPTS}.0.user", request_body.get("prompt")
        )
        _set_span_attribute(
            span,
            f"{SpanAttributes.LLM_COMPLETIONS}.0.content",
            response_body.get("completion"),
        )


def _set_anthropic_messages_span_attributes(span, request_body, response_body):
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TYPE, LLMRequestTypeValues.CHAT.value
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TOP_P, request_body.get("top_p")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TEMPERATURE, request_body.get("temperature")
    )
    _set_span_attribute(
        span,
        SpanAttributes.LLM_REQUEST_MAX_TOKENS,
        request_body.get("max_tokens"),
    )

    prompt_tokens = 0
    completion_tokens = 0
    if (
        response_body.get("usage") is not None
        and response_body.get("usage").get("input_tokens") is not None
        and response_body.get("usage").get("output_tokens") is not None
    ):
        prompt_tokens = response_body.get("usage").get("input_tokens")
        completion_tokens = response_body.get("usage").get("output_tokens")
        _record_usage_to_span(span, prompt_tokens, completion_tokens)
    elif response_body.get("invocation_metrics") is not None:
        prompt_tokens = response_body.get("invocation_metrics").get("inputTokenCount")
        completion_tokens = response_body.get("invocation_metrics").get(
            "outputTokenCount"
        )
        _record_usage_to_span(span, prompt_tokens, completion_tokens)
    elif Config.enrich_token_usage:
        messages = [message.get("content") for message in request_body.get("messages")]

        raw_messages = []
        for message in messages:
            if isinstance(message, str):
                raw_messages.append(message)
            else:
                raw_messages.extend([content.get("text") for content in message])
        prompt_tokens = _count_anthropic_tokens(raw_messages)
        completion_tokens = _count_anthropic_tokens(
            [content.get("text") for content in response_body.get("content")]
        )
        _record_usage_to_span(span, prompt_tokens, completion_tokens)

    if should_send_prompts():
        for idx, message in enumerate(request_body.get("messages")):
            _set_span_attribute(
                span, f"{SpanAttributes.LLM_PROMPTS}.{idx}.role", message.get("role")
            )
            _set_span_attribute(
                span,
                f"{SpanAttributes.LLM_PROMPTS}.0.content",
                json.dumps(message.get("content")),
            )

        _set_span_attribute(
            span, f"{SpanAttributes.LLM_COMPLETIONS}.0.content", "assistant"
        )
        _set_span_attribute(
            span,
            f"{SpanAttributes.LLM_COMPLETIONS}.0.content",
            json.dumps(response_body.get("content")),
        )


def _count_anthropic_tokens(messages: list[str]):
    count = 0
    for message in messages:
        count += anthropic_client.count_tokens(text=message)
    return count


def _set_ai21_span_attributes(span, request_body, response_body):
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TYPE, LLMRequestTypeValues.COMPLETION.value
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TOP_P, request_body.get("topP")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TEMPERATURE, request_body.get("temperature")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_MAX_TOKENS, request_body.get("maxTokens")
    )

    _record_usage_to_span(
        span,
        len(response_body.get("prompt").get("tokens")),
        len(response_body.get("completions")[0].get("data").get("tokens")),
    )

    if should_send_prompts():
        _set_span_attribute(
            span, f"{SpanAttributes.LLM_PROMPTS}.0.user", request_body.get("prompt")
        )

        for i, completion in enumerate(response_body.get("completions")):
            _set_span_attribute(
                span,
                f"{SpanAttributes.LLM_COMPLETIONS}.{i}.content",
                completion.get("data").get("text"),
            )


def _set_llama_span_attributes(span, request_body, response_body):
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TYPE, LLMRequestTypeValues.COMPLETION.value
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TOP_P, request_body.get("top_p")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_TEMPERATURE, request_body.get("temperature")
    )
    _set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_MAX_TOKENS, request_body.get("max_gen_len")
    )

    _record_usage_to_span(
        span,
        response_body.get("prompt_token_count"),
        response_body.get("generation_token_count"),
    )

    if should_send_prompts():
        _set_span_attribute(
            span, f"{SpanAttributes.LLM_PROMPTS}.0.user", request_body.get("prompt")
        )

        for i, generation in enumerate(response_body.get("generations")):
            _set_span_attribute(
                span, f"{SpanAttributes.LLM_COMPLETIONS}.{i}.content", response_body
            )


class BedrockInstrumentor(BaseInstrumentor):
    """An instrumentor for Bedrock's client library."""

    def __init__(self, enrich_token_usage: bool = False, exception_logger=None):
        super().__init__()
        Config.enrich_token_usage = enrich_token_usage
        Config.exception_logger = exception_logger

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        tracer = get_tracer(__name__, __version__, tracer_provider)
        for wrapped_method in WRAPPED_METHODS:
            wrap_package = wrapped_method.get("package")
            wrap_object = wrapped_method.get("object")
            wrap_method = wrapped_method.get("method")
            wrap_function_wrapper(
                wrap_package,
                f"{wrap_object}.{wrap_method}",
                _wrap(tracer, wrapped_method),
            )

    def _uninstrument(self, **kwargs):
        for wrapped_method in WRAPPED_METHODS:
            wrap_package = wrapped_method.get("package")
            wrap_object = wrapped_method.get("object")
            unwrap(
                f"{wrap_package}.{wrap_object}",
                wrapped_method.get("method"),
            )

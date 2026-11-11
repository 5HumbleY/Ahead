from dataclasses import dataclass

from .model import Model


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    api: str


MODELS = {
    "Qwen3.5-9B": ModelSpec("Qwen/Qwen3.5-9B", "chat"),
    "Qwen3.8-27B": ModelSpec("Qwen/Qwen3.8-27B", "chat"),
    "GPT-6.1 Terra": ModelSpec("gpt-6.1-terra", "responses"),
}


def create_model(
    name,
    endpoint,
    directory,
    *,
    served_model=None,
    concurrency=2,
    max_calls=None,
    **options,
):
    if name not in MODELS:
        raise ValueError("Unknown model: " + str(name))
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("A model endpoint is required")
    spec = MODELS[name]
    config = {
        "thinking": True,
        "reasoning_effort": "medium",
        "context_limit": 65536,
        "max_completion_tokens": 32768,
    }
    if spec.api == "responses":
        config.update(responses_format="json_object", service_tier="default")
    else:
        config.update(vllm=True, native_function_calls=True)
    config.update(options)
    config.update(
        model=served_model or spec.model_id,
        model_api=spec.api,
        inference_url=endpoint,
    )
    return Model(config, directory, concurrency=concurrency, max_calls=max_calls)

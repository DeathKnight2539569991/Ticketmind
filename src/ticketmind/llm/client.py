from openai import OpenAI
from ticketmind.core.config import QwenSettings
GENERATION_OPTIONS = {
    "temperature": 0.2,
    "max_tokens": 512,
    "extra_body": {"enable_thinking": False},
}
def generate_text(
        *,
        settings: QwenSettings,
        system_prompt: str,
        user_prompt: str,
        json_mode:bool=False,
        timeout:float=30,
        generation_options:dict | None=None,
        usage_callback=None,
        response_callback=None,
):
    base_url = (
        f"https://{settings.workspace_id}.cn-beijing.maas.aliyuncs.com"
        "/compatible-mode/v1"
    )
    with OpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    ) as client:
        completion = client.chat.completions.create(
            model=settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **(GENERATION_OPTIONS if generation_options is None else generation_options),
            response_format=(
    {"type": "json_object"}
    if json_mode
    else {"type": "text"}
),
        )
    if response_callback is not None:
        response_callback({"request_id": completion.id,
                           "usage": completion.usage.model_dump() if completion.usage is not None else None,
                           "choices": [{"content": choice.message.content, "finish_reason": choice.finish_reason}
                                       for choice in completion.choices]})
    if usage_callback is not None:
        usage_callback(completion.usage.model_dump() if completion.usage is not None else None)
    if not completion.choices:
        raise ValueError("No choices returned from the API.")
    choice = completion.choices[0]
    if choice.finish_reason != "stop":
        raise ValueError(f"Unexpected finish reason: {choice.finish_reason}")
    content=choice.message.content
    if content is None or not content.strip():
        raise ValueError("Empty content returned from the API.")
    return content.strip() 

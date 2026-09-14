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
        json_mode:bool=False
):
    base_url = (
        f"https://{settings.workspace_id}.cn-beijing.maas.aliyuncs.com"
        "/compatible-mode/v1"
    )
    with OpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=base_url,
        timeout=30,
        max_retries=0,
    ) as client:
        completion = client.chat.completions.create(
            model=settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **GENERATION_OPTIONS,
            response_format=(
    {"type": "json_object"}
    if json_mode
    else {"type": "text"}
),
        )
    if not completion.choices:
        raise ValueError("No choices returned from the API.")
    choice = completion.choices[0]
    if choice.finish_reason != "stop":
        raise ValueError(f"Unexpected finish reason: {choice.finish_reason}")
    content=choice.message.content
    if content is None or not content.strip():
        raise ValueError("Empty content returned from the API.")
    return content.strip() 

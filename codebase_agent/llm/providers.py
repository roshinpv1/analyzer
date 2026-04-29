import os
import httpx
import json
import uuid
import datetime
import base64
import re
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Any, Dict, Sequence, Union, List, Mapping, Literal

from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    SystemMessage,
    UserMessage,
    AssistantMessage,
    RequestUsage,
)

class LLMProvider(Enum):
    LOCAL = "local"
    APIGEE = "apigee"

@dataclass
class LLMConfig:
    provider: LLMProvider
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 4096
    context_window: int = 0
    timeout: float = 600.0

    @property
    def effective_context_window(self) -> int:
        if self.context_window > 0:
            return self.context_window
        return self.max_tokens * 4

class LLMDriver(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

# --- Token Manager ---

class ApigeeTokenManager:
    def __init__(self):
        self.token: Optional[str] = None
        self.token_expiry: Optional[datetime.datetime] = None

    async def get_token(self) -> str:
        if self.token and self.token_expiry and datetime.datetime.now() < self.token_expiry:
            return self.token

        token = await self._fetch_token()
        self.token = token
        self.token_expiry = datetime.datetime.now() + datetime.timedelta(hours=1)
        return self.token

    async def _fetch_token(self) -> str:
        login_url = os.environ.get("APIGEE_NONPROD_LOGIN_URL")
        consumer_key = os.environ.get("APIGEE_CONSUMER_KEY")
        consumer_secret = os.environ.get("APIGEE_CONSUMER_SECRET")

        if not all([login_url, consumer_key, consumer_secret]):
            raise ValueError("Apigee OAuth credentials not configured.")

        auth_str = f"{consumer_key}:{consumer_secret}"
        base64_auth = base64.b64encode(auth_str.encode()).decode()

        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(
                login_url,
                headers={
                    "Authorization": f"Basic {base64_auth}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={"grant_type": "client_credentials"}
            )
            response.raise_for_status()
            return response.json().get("access_token")

    def clear_token(self):
        self.token = None
        self.token_expiry = None

# --- Drivers ---

def _safe_extract_message(data: dict) -> dict:
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return {}
    first = choices[0]
    if not isinstance(first, dict):
        return {}
    msg = first.get("message")
    if not isinstance(msg, dict):
        return {}
    return msg

class LocalDriver(LLMDriver):
    def __init__(self, config: LLMConfig):
        self.config = config

    @staticmethod
    def _detect_and_truncate_repetition(text: str, min_phrase_len: int = 15, max_repeats: int = 4) -> str:
        if len(text) < min_phrase_len * max_repeats:
            return text
        tail = text[-2000:]
        for phrase_len in range(min_phrase_len, min(100, len(tail) // max_repeats)):
            candidate = tail[-phrase_len:]
            count = 0
            pos = len(tail)
            while pos >= phrase_len:
                segment = tail[pos - phrase_len:pos]
                if segment == candidate:
                    count += 1
                    pos -= phrase_len
                else:
                    break
            if count >= max_repeats:
                repeat_start = text.rfind(candidate * 2)
                if repeat_start > 0:
                    return text[:repeat_start].rstrip(", \n")
        return text

    async def generate(self, prompt: str, **kwargs) -> str:
        base_url = self.config.base_url or "http://localhost:1234/v1"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key and self.config.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        messages = []
        system_prompt = kwargs.pop("system_prompt", None)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        max_tokens = kwargs.get("max_tokens", self.config.max_tokens)
        temperature = kwargs.get("temperature", self.config.temperature)

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.config.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": min(max_tokens, self.config.max_tokens),
                }
            )
            response.raise_for_status()
            data = response.json()
            msg = _safe_extract_message(data)
            output = msg.get("content") or ""
            
            output = re.sub(r'<think>.*?</think>', '', output, flags=re.DOTALL).strip()
            output = self._detect_and_truncate_repetition(output)
            return output

    def is_available(self) -> bool:
        return bool(self.config.base_url)

class ApigeeDriver(LLMDriver):
    def __init__(self, config: LLMConfig):
        self.config = config
        self.token_manager = ApigeeTokenManager()

    async def generate(self, prompt: str, **kwargs) -> str:
        token = await self.token_manager.get_token()
        
        enterprise_base_url = (self.config.base_url or os.environ.get("ENTERPRISE_BASE_URL", "")).rstrip("/")
        wf_use_case_id = os.environ.get("WF_USE_CASE_ID")
        wf_client_id = os.environ.get("WF_CLIENT_ID")
        wf_api_key = os.environ.get("WF_API_KEY")

        if not all([enterprise_base_url, wf_use_case_id, wf_client_id, wf_api_key]):
            raise ValueError("Apigee enterprise configuration incomplete")

        if enterprise_base_url.endswith("/v1/chat/completions"):
            api_url = enterprise_base_url
        elif enterprise_base_url.endswith("/v1"):
            api_url = f"{enterprise_base_url}/chat/completions"
        else:
            api_url = f"{enterprise_base_url}/v1/chat/completions"

        messages = []
        system_prompt = kwargs.pop("system_prompt", None)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        apigee_max_output = int(os.environ.get("APIGEE_MAX_OUTPUT_TOKENS", "8192"))
        max_tokens = min(kwargs.get("max_tokens", self.config.max_tokens), apigee_max_output)

        headers = {
            "x-wf-request-date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "Authorization": f"Bearer {token}",
            "x-request-id": str(uuid.uuid4()),
            "x-correlation-id": str(uuid.uuid4()),
            "X-WF-client-id": wf_client_id,
            "X-WF-api-key": wf_api_key,
            "X-WF-usecase-id": wf_use_case_id,
            "Content-Type": "application/json"
        }

        request_body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": max_tokens
        }

        async with httpx.AsyncClient(timeout=self.config.timeout, verify=False) as client:
            response = await client.post(api_url, headers=headers, json=request_body)
            
            if response.status_code == 401:
                self.token_manager.clear_token()
                token = await self.token_manager.get_token()
                headers["Authorization"] = f"Bearer {token}"
                response = await client.post(api_url, headers=headers, json=request_body)

            response.raise_for_status()
            data = response.json()
            return _safe_extract_message(data).get("content", "")

    def is_available(self) -> bool:
        return bool(os.environ.get("ENTERPRISE_BASE_URL"))

# --- Factory ---

def get_llm_client() -> LLMDriver:
    provider_env = os.environ.get("LLM_PROVIDER", "local").lower()
    
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.1"))
    
    if provider_env == "apigee":
        config = LLMConfig(
            provider=LLMProvider.APIGEE,
            model=os.environ.get("APIGEE_MODEL", "gpt-4"),
            base_url=os.environ.get("ENTERPRISE_BASE_URL"),
            timeout=float(os.environ.get("APIGEE_TIMEOUT", "600")),
            temperature=float(os.environ.get("APIGEE_TEMPERATURE", str(temperature))),
            max_tokens=max_tokens
        )
        return ApigeeDriver(config)
    
    # Default to Local
    config = LLMConfig(
        provider=LLMProvider.LOCAL,
        model=os.environ.get("LOCAL_LLM_MODEL", "openai/gpt-oss-20b"),
        base_url=os.environ.get("LOCAL_LLM_URL", "http://localhost:1234/v1"),
        api_key=os.environ.get("LOCAL_LLM_API_KEY", "not-needed"),
        temperature=temperature,
        max_tokens=max_tokens
    )
    return LocalDriver(config)

# --- AutoGen Wrapper ---

class AutoGenLLMWrapper(ChatCompletionClient):
    """Wraps our custom LLMDriver to conform to AutoGen's ChatCompletionClient interface."""
    
    def __init__(self, driver: LLMDriver):
        self.driver = driver
        
    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Any] = [],
        json_output: Optional[Union[bool, type]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[Any] = None,
        **kwargs: Any
    ) -> CreateResult:
        # 1. Parse AutoGen messages to prompt & system_prompt
        system_parts = []
        user_parts = []
        
        for msg in messages:
            if isinstance(msg, SystemMessage):
                if isinstance(msg.content, str):
                    system_parts.append(msg.content)
            elif isinstance(msg, UserMessage):
                if isinstance(msg.content, str):
                    user_parts.append(msg.content)
            elif isinstance(msg, AssistantMessage):
                if isinstance(msg.content, str):
                    user_parts.append(f"Assistant: {msg.content}")

        system_prompt = "\n\n".join(system_parts) if system_parts else None
        prompt = "\n\n".join(user_parts)
        
        # 2. Call custom driver
        content = await self.driver.generate(
            prompt,
            system_prompt=system_prompt,
            **extra_create_args
        )
        
        # 3. Wrap in AutoGen CreateResult
        usage = RequestUsage(prompt_tokens=0, completion_tokens=0)
        return CreateResult(
            finish_reason="stop",
            content=content,
            usage=usage,
            cached=False
        )

    def remaining_tokens(self, messages: Sequence[LLMMessage]) -> int:
        return self.driver.config.max_tokens

    @property
    def capabilities(self) -> Any:
        # Mocking capabilities for AutoGen compatibility
        class Capabilities:
            vision = False
            function_calling = False
            json_output = True
        return Capabilities()

    def count_tokens(self, messages: Sequence[LLMMessage]) -> int:
        return 0

    def actual_usage(self) -> RequestUsage:
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def total_usage(self) -> RequestUsage:
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def close(self) -> None:
        pass

    async def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Any] = [],
        json_output: Optional[Union[bool, type]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[Any] = None,
        **kwargs: Any
    ):
        # Delegate to standard create, yield content then final CreateResult
        result = await self.create(
            messages=messages,
            tools=tools,
            json_output=json_output,
            extra_create_args=extra_create_args,
            cancellation_token=cancellation_token,
            **kwargs
        )
        # Yield the generated text string first
        if isinstance(result.content, str):
            yield result.content
        # Finally yield the CreateResult
        yield result

    @property
    def model_info(self) -> dict[str, Any]:
        return {
            "family": "unknown",
            "vision": False,
            "function_calling": False,
            "json_output": True,
            "structured_output": False,
        }

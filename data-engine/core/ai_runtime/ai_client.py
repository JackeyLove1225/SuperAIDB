"""AI 客户端封装——统一调用 DeepSeek / GPT API，带重试和降级"""

import json
import os
import time
from typing import Optional

from core.logger import info as log_info, warning as log_warning, error as log_error


class AIClient:
    """统一 AI 调用封装，支持 DeepSeek 和 OpenAI 兼容 API"""

    MAX_RETRIES = 3
    RETRY_DELAY = 2  # 初始延迟秒数

    # 全局单例（避免多处代码各自新建 OpenAI client + httpx 连接池）
    _instance: "AIClient" = None

    @classmethod
    def get_instance(cls) -> "AIClient":
        """获取全局 AIClient 单例

        性能优化：复用 OpenAI client（httpx 连接池），避免重复初始化
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        self.api_key = api_key or os.getenv("AI_API_KEY", "")
        if not self.api_key:
            from pathlib import Path
            env_path = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
            if env_path.exists():
                from dotenv import load_dotenv
                load_dotenv(env_path)
                self.api_key = os.getenv("AI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "AI_API_KEY 未配置。请创建 data-engine/config/.env 文件，"
                "写入 AI_API_KEY=你的DeepSeekKey"
            )
        self.base_url = base_url or os.getenv("AI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("AI_MODEL", "deepseek-v4-flash")
        # 懒导入 OpenAI（节省 ~41MB 模块导入内存；只在首次实例化时加载）
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _call(self, messages: list[dict],
              temperature: float, max_tokens: int) -> str:
        """带指数退避重试的 API 调用"""
        last_err = None
        # DeepSeek v4 默认开启思考模式：推理 tokens 会吃光 max_tokens 预算，
        # 导致 content 返回空字符串（提取类任务因此整批静默丢失）。
        # 本客户端全部服务于工具型确定性任务（路由/提取/映射），不需要推理，
        # 与 call_function 一致，在唯一收口处统一关闭思考。
        extra = {}
        if self.model.startswith("deepseek-v4"):
            extra["extra_body"] = {"thinking": {"type": "disabled"}}
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                t0 = time.time()
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
                cost = time.time() - t0
                self._record(response)
                content = response.choices[0].message.content or ""
                if attempt > 1:
                    log_info("AI 重试成功", model=self.model, attempt=attempt, cost=f"{cost:.1f}s")
                return content
            except Exception as e:
                last_err = e
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY * (2 ** (attempt - 1))
                    log_warning("AI 调用失败，准备重试", model=self.model,
                                   attempt=attempt, error=str(e)[:60], delay=delay)
                    time.sleep(delay)
                else:
                    log_error("AI 调用最终失败", model=self.model, error=str(e)[:100])

        raise RuntimeError(f"AI 调用失败（重试{self.MAX_RETRIES}次）: {last_err}")

    def _record(self, response) -> None:
        """token 用量落账（3.0 统计；fail-open）——DeepSeek usage 含
        prompt_cache_hit_tokens（前缀缓存命中，低价计费）"""
        try:
            from core.llm_usage import record_usage, current_role
            u = getattr(response, "usage", None)
            if not u:
                return
            record_usage(current_role(), self.model,
                         getattr(u, "prompt_tokens", None),
                         getattr(u, "completion_tokens", None),
                         getattr(u, "prompt_cache_hit_tokens", None))
        except Exception:
            pass

    def chat(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.1, max_tokens: int = 4096) -> str:
        """调用 AI 对话（单轮）"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self._call(messages, temperature, max_tokens)

    def chat_messages(self, messages: list[dict],
                      temperature: float = 0.1, max_tokens: int = 4096) -> str:
        """多轮对话——调用 AI 并传入完整的消息历史"""
        return self._call(messages, temperature, max_tokens)

    def chat_json(self, system_prompt: str, user_prompt: str,
                  temperature: float = 0.1) -> dict:
        """调用 AI 并期望返回 JSON"""
        content = self.chat(system_prompt, user_prompt, temperature)
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)

    def call_function(self, functions: list[dict], user_prompt: str,
                       system_prompt: str = "",
                       temperature: float = 0.05,
                       max_tokens: int = 2000) -> tuple[str, dict]:
        """Function Calling——返回 (tool_name, args_dict)"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        # DeepSeek v4 思考模式与 tool_choice=required 冲突（400），
        # 显式关闭思考（实测 thinking=disabled + required 正常返回 tool_calls）；
        # openai SDK 不收顶层 thinking 参数，必须走 extra_body 透传
        extra = {}
        if self.model.startswith("deepseek-v4"):
            extra["extra_body"] = {"thinking": {"type": "disabled"}}
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=functions,
            tool_choice="required",
            temperature=temperature,
            max_tokens=max_tokens,
            **extra,
        )

        msg = response.choices[0].message
        self._record(response)

        if msg.tool_calls:
            tc = msg.tool_calls[0]
            if tc.function.arguments:
                # 容错处理：AI 偶尔返回多个 JSON 对象拼接或尾部多余字符
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    decoder = json.JSONDecoder()
                    args, _ = decoder.raw_decode(tc.function.arguments)
            else:
                args = {}
            return tc.function.name, args

        return "", {"text": msg.content or ""}

"""Gemini REST client. Credentials stay in memory and authentication headers."""
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class Config:
    api_key: str = field(repr=False)
    model: str = "auto"


def load_config(root: Path, key_file=None, model=None):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    configured_model = os.environ.get("GEMINI_MODEL")
    files = [Path(key_file)] if key_file else [root / ".env"]
    for resources in (root / "resources", root / "output/resources"):
        if key_file or not resources.is_dir():
            continue
        files += sorted(path for path in resources.iterdir()
                        if path.is_file() and path.suffix.lower() in (".json", ".txt", ".md", ".env", ".yaml", ".yml")
                        and ("gem" in path.name.lower() or "key" in path.name.lower() or path.name == ".env")
                        and "example" not in path.name.lower())
    for path in files:
        if not path.is_file() or path.stat().st_size > 100_000:
            continue
        content = path.read_text(encoding="utf-8-sig")
        values = {}
        if path.suffix.lower() == ".json":
            values = json.loads(content)
            if not isinstance(values, dict):
                raise ValueError("Gemini config JSON must be an object")
        else:
            for line in content.splitlines():
                match = re.match(r"\s*(?:export\s+)?([A-Za-z_]+)\s*[:=]\s*(.*?)\s*$", line)
                if match:
                    values[match[1]] = match[2].strip("\"'")
        if not key:
            key = values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY") or values.get("api_key") or values.get("API_KEY") or values.get("key")
            if not key:
                keys = re.findall(r"AIza[0-9A-Za-z_-]{30,}", content)
                if len(set(keys)) == 1:
                    key = keys[0]
            if not key:
                bare = content.strip().strip("\"'")
                if re.fullmatch(r"[A-Za-z0-9_.-]{20,200}", bare):
                    key = bare
                else:
                    labeled = re.search(r"(?i)(?:gemini|gemina|google)[ _-]*(?:api[ _-]*)?key\s*[:=]\s*[\"']?([A-Za-z0-9_.-]{20,200})", content)
                    if labeled:
                        key = labeled[1]
        configured_model = configured_model or values.get("GEMINI_MODEL") or values.get("model")
        if key:
            break
    if not key or not isinstance(key, str) or "YOUR_" in key:
        raise ValueError("未找到 Gemini key。设置 GEMINI_API_KEY，或使用 --key-file 指定 resources 中的密钥文件。")
    chosen = model or configured_model or "auto"
    if chosen != "auto" and not re.fullmatch(r"[A-Za-z0-9_.-]+", chosen):
        raise ValueError("Invalid Gemini model ID")
    return Config(key.strip(), chosen)


class GeminiClient:
    provider = "gemini"
    def __init__(self, config, opener=urlopen):
        self.config = config
        self.opener = opener
        self.model = config.model

    def _request(self, endpoint, payload=None):
        raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = Request(API + endpoint, data=raw, headers={
            "x-goog-api-key": self.config.api_key,
            "Content-Type": "application/json", "Accept": "application/json"})
        for attempt in range(2):
            try:
                with self.opener(request, timeout=90) as response:
                    return json.loads(response.read())
            except HTTPError as exc:
                if attempt == 0 and exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2)
                    continue
                # Avoid printing request headers, request URLs with credentials or response bodies.
                labels = {400: "请求参数或 API key 无效", 401: "认证失败", 403: "key 权限或访问地区受限",
                          404: "模型不存在或不可用", 429: "配额或速率限制，请稍后重试"}
                detail = ""
                try:
                    detail = json.loads(exc.read()).get("error", {}).get("message", "")
                    detail = str(detail).replace(self.config.api_key, "[redacted]")[:400]
                except (ValueError, AttributeError):
                    pass
                raise RuntimeError(f"Gemini HTTP {exc.code}: {labels.get(exc.code, '服务调用失败')}" + (f" — {detail}" if detail else "")) from None
            except (URLError, TimeoutError):
                if attempt == 0 and payload is None:
                    time.sleep(1)
                    continue
                raise RuntimeError("Gemini 连接失败或请求超过90秒；请检查网络或稍后重试。未生成模型匹配结果。") from None

    def resolve_model(self):
        if self.model != "auto":
            return self.model
        models = self._request("/models?pageSize=1000").get("models", [])
        names = {item["name"].removeprefix("models/") for item in models
                 if "generateContent" in item.get("supportedGenerationMethods", [])}
        for preferred in ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"):
            if preferred in names:
                self.model = preferred
                return preferred
        choices = sorted(name for name in names if "gemini" in name and "flash" in name
                         and not any(part in name for part in ("image", "audio", "live", "tts", "robotics")))
        if not choices:
            raise RuntimeError("未找到可用的 Gemini Flash 文本模型，请通过 --model 指定账户可用模型。")
        self.model = choices[0]
        return self.model

    def generate(self, system, prompt, schema):
        model = self.resolve_model()
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 12000,
                                 "responseMimeType": "application/json", "responseJsonSchema": schema},
        }
        response = self._request(f"/models/{quote(model, safe='')}:generateContent", payload)
        candidates = response.get("candidates") or []
        if not candidates or candidates[0].get("finishReason") not in (None, "STOP"):
            raise RuntimeError("Gemini 未返回完整结果（可能被过滤或输出截断），请减少候选岗位数。")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        try:
            result = json.loads(text)
        except ValueError:
            raise RuntimeError("Gemini 返回的结果不是有效 JSON，未保存为匹配结果。") from None
        return result, {"provider": "gemini", "model": response.get("modelVersion", model), "usage": response.get("usageMetadata", {})}

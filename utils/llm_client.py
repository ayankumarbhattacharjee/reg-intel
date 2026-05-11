"""Shared Azure OpenAI LLM client factory.

Credential resolution order (first wins):
  1. Environment variables  AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, etc.
  2. config.json  (local dev / on-prem; never commit the real key)
"""
import json
import os
from pathlib import Path

_CONFIG: dict | None = None


def _load_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        config_path = Path(__file__).parent.parent / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
        else:
            # No config.json — build a minimal config from env vars.
            _CONFIG = {
                "azure_openai": {},
                "uc1": {"faiss_index_path": "uc1_faiss_index", "assets_path": "assets/uc1",
                        "top_k": 5, "pages_returned": 3, "qte_temperature": 0.5,
                        "qte_max_tokens": 200, "rag_temperature": 0.6, "rag_max_tokens": 2000},
                "uc2": {"assets_path": "assets/uc2", "chunk_size": 10000, "chunk_overlap": 1000},
                "uc3": {"assets_path": "assets/uc3", "chunk_size": 10000, "chunk_overlap": 1000},
            }

        # Env vars override anything in config.json (deployment-safe credential injection)
        az = _CONFIG.setdefault("azure_openai", {})
        for env_key, cfg_key in [
            ("AZURE_OPENAI_API_KEY",        "api_key"),
            ("AZURE_OPENAI_ENDPOINT",        "endpoint"),
            ("AZURE_OPENAI_API_VERSION",     "api_version"),
            ("AZURE_CHAT_DEPLOYMENT",        "chat_deployment"),
            ("AZURE_MINI_DEPLOYMENT",        "mini_deployment"),
            ("AZURE_EMBEDDING_DEPLOYMENT",   "embedding_deployment"),
        ]:
            val = os.environ.get(env_key)
            if val:
                az[cfg_key] = val

    return _CONFIG


def get_config() -> dict:
    return _load_config()


def get_chat_llm(model_key: str = "mini_deployment", temperature: float = 0, max_tokens: int | None = None):
    """
    Return a LangChain AzureChatOpenAI instance.
    model_key: 'chat_deployment' (gpt-4o) or 'mini_deployment' (gpt-4o-mini)
    """
    from langchain_openai import AzureChatOpenAI

    cfg = _load_config()
    az = cfg["azure_openai"]
    deployment = az.get(model_key, az["mini_deployment"])

    kwargs = dict(
        openai_api_key=az["api_key"],
        azure_endpoint=az["endpoint"],
        openai_api_version=az["api_version"],
        azure_deployment=deployment,
        temperature=temperature,
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return AzureChatOpenAI(**kwargs)


def get_embeddings():
    """Return a LangChain AzureOpenAIEmbeddings instance."""
    from langchain_openai import AzureOpenAIEmbeddings

    cfg = _load_config()
    az = cfg["azure_openai"]
    return AzureOpenAIEmbeddings(
        openai_api_key=az["api_key"],
        azure_endpoint=az["endpoint"],
        openai_api_version=az["api_version"],
        azure_deployment=az["embedding_deployment"],
    )


def read_asset(assets_path: str, filename: str) -> str:
    """Read a markdown asset file from the given assets directory."""
    fp = Path(assets_path) / f"{filename}.md"
    try:
        return fp.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"Asset not found: {fp}")

"""Server-side chart analyst: Ollama Cloud + chart skill lenses."""

from .config import CHAT_MODELS, ChatConfig, ChatConfigError, public_models, save_chat_model
from .context import ChartContext, ChartContextError, build_chart_context, clear_context_cache
from .ollama import OllamaCloudClient, OllamaCloudError, OllamaReply
from .service import ChatRequestError, build_messages, generate_reply
from .skills import CHART_SKILLS, MAX_SKILLS, ChartSkill, public_skills, select_skills
from .tools import TOOL_SPECS, ToolRuntime

__all__ = [
    "CHART_SKILLS",
    "CHAT_MODELS",
    "MAX_SKILLS",
    "TOOL_SPECS",
    "ChartConfig",
    "ChartConfigError",
    "ChartContext",
    "ChartContextError",
    "ChartSkill",
    "ChatRequestError",
    "OllamaCloudClient",
    "OllamaCloudError",
    "OllamaReply",
    "ToolRuntime",
    "build_chart_context",
    "build_messages",
    "clear_context_cache",
    "generate_reply",
    "public_models",
    "public_skills",
    "save_chat_model",
    "select_skills",
]

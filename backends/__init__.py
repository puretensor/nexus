"""Backend factory — lazy singleton keyed by ENGINE_BACKEND env var."""

import logging

log = logging.getLogger("nexus")

_backend_instance = None


def get_backend():
    """Return the configured backend (lazy singleton).

    Backend is selected by config.ENGINE_BACKEND (default: 'ollama').
    Instance is cached after first creation.
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    from config import ENGINE_BACKEND

    _REGISTRY = {
        "ollama": ("backends.ollama", "OllamaBackend"),
        "claude_code": ("backends.claude_code", "ClaudeCodeBackend"),
        "anthropic_api": ("backends.anthropic_api", "AnthropicAPIBackend"),
        "codex_cli": ("backends.codex_cli", "CodexCLIBackend"),
        "gemini_cli": ("backends.gemini_cli", "GeminiCLIBackend"),
    }

    if ENGINE_BACKEND not in _REGISTRY:
        raise ValueError(
            f"Unknown ENGINE_BACKEND: {ENGINE_BACKEND!r}. "
            f"Valid options: {', '.join(sorted(_REGISTRY))}"
        )

    module_path, class_name = _REGISTRY[ENGINE_BACKEND]

    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    _backend_instance = cls()

    log.info("Initialized backend: %s", _backend_instance.name)
    return _backend_instance


def reset_backend():
    """Reset the singleton (for testing)."""
    global _backend_instance
    _backend_instance = None

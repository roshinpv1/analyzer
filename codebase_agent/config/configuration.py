"""Configuration management for the AutoGen Codebase Understanding Agent.

This module provides secure and flexible configuration management supporting multiple
OpenAI-compatible API providers including OpenAI, OpenRouter, and LiteLLM.

Note: This implementation relies on AutoGen 0.7.4's internal model definitions
(_MODEL_TOKEN_LIMITS) for fuzzy model matching and token limit detection.
The AutoGen version is pinned to ensure consistent behavior.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """Configuration for LLM API settings."""

    api_key: str
    base_url: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 4000
    timeout: int = 60


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing."""

    pass


class ConfigurationManager:
    """Manages environment configuration and LLM settings for AutoGen agents.

    Supports multiple OpenAI-compatible API providers and provides validation
    with clear error messages for missing or invalid configuration.
    """

    # Required environment variables
    REQUIRED_KEYS = {}

    # Optional environment variables with defaults
    OPTIONAL_KEYS = {
        "MODEL_TEMPERATURE": "0.1",
        "MAX_TOKENS": "4000",
        "REQUEST_TIMEOUT": "60",
        "AGENT_TIMEOUT": "300",
        "MAX_SHELL_OUTPUT_SIZE": "10000",
        "LOG_LEVEL": "INFO",
        "DEBUG": "false",
        "ALLOWED_WORKING_DIRECTORY": "",
        "MODEL_FAMILY": "openai",
        "MODEL_VISION": "false",
        "MODEL_FUNCTION_CALLING": "true",
        "MODEL_JSON_OUTPUT": "true",
        "MODEL_STRUCTURED_OUTPUT": "false",
    }

    # Default values for common API providers
    DEFAULT_BASE_URLS = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "litellm": "http://localhost:4000",
    }

    def __init__(self, project_root: Path | None = None):
        """Initialize configuration manager.

        Args:
            project_root: Path to project root directory. If None, uses current directory.
        """
        self.project_root = project_root or Path.cwd()
        self.env_file = self._find_env_file()
        self._config: dict[str, str] = {}
        self._is_loaded = False
        self.logger = logging.getLogger(__name__)

    def _find_env_file(self) -> Path:
        """Find the most appropriate .env file.

        Searches in:
        1. The provided project_root
        2. The current working directory
        3. The package root directory (fallback)

        Returns:
            Path to the discovered .env file or default path in project_root.
        """
        # 1. Try project root
        env_in_project = self.project_root / ".env"
        if env_in_project.exists():
            return env_in_project

        # 2. Try current working directory
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            return cwd_env

        # 3. Try package root
        try:
            import codebase_agent

            package_root = Path(codebase_agent.__file__).parent.parent
            package_env = package_root / ".env"
            if package_env.exists():
                return package_env
        except ImportError:
            pass

        # Fallback to default path
        return env_in_project

    def load_environment(self) -> None:
        """Load environment variables from .env file and system environment.

        Raises:
            ConfigurationError: If .env file exists but cannot be loaded.
        """
        try:
            # Load from .env file if it exists
            if self.env_file.exists():
                success = load_dotenv(self.env_file)
                if not success:
                    logger.warning(f"Failed to load .env file from {self.env_file}")
                else:
                    logger.info(f"Loaded configuration from {self.env_file}")
            else:
                logger.warning(
                    f"No .env file found at {self.env_file}. Using system environment only."
                )

            # Load all environment variables into our config
            self._config = dict(os.environ)
            self._is_loaded = True

            logger.debug(f"Configuration loaded with {len(self._config)} variables")

        except Exception as e:
            raise ConfigurationError(
                f"Failed to load environment configuration: {e}"
            ) from e

    def validate_configuration(self) -> list[str]:
        """Validate required configuration values.

        Returns:
            List of missing or invalid configuration keys.
        """
        if not self._is_loaded:
            self.load_environment()

        missing_keys = []

        # Check required keys
        for key, description in self.REQUIRED_KEYS.items():
            value = self._config.get(key)
            if not value or value.strip() == "":
                missing_keys.append(f"{key} ({description})")



        # Validate numeric values
        for key in [
            "MODEL_TEMPERATURE",
            "MAX_TOKENS",
            "REQUEST_TIMEOUT",
            "AGENT_TIMEOUT",
            "MAX_SHELL_OUTPUT_SIZE",
        ]:
            value = self._config.get(key)
            if value and not self._is_valid_numeric(value):
                missing_keys.append(f"{key} (must be a valid number)")

        return missing_keys



    def get_model_client(self):
        """Get ChatCompletionClient for new AutoGen API.

        Integrates the consolidated codebase_agent.llm module
        directly into the analysis pipeline.

        Returns:
            ChatCompletionClient instance.
        """
        from ..llm.providers import get_llm_client, AutoGenLLMWrapper
        
        # Instantiate the custom LLM driver (Local or Apigee)
        llm_driver = get_llm_client()
        
        # Wrap it in the AutoGen ChatCompletionClient interface
        return AutoGenLLMWrapper(driver=llm_driver)



    def get_model_info(self) -> dict[str, Any]:
        """Get model information configuration for AutoGen API.

        Returns:
            Dictionary with model capabilities and settings.
        """
        return {
            "family": "local",
            "vision": False,
            "function_calling": False,
            "json_output": True,
            "structured_output": False,
        }

    def get_agent_config(self) -> dict[str, Any]:
        """Get agent-specific configuration settings.

        Returns:
            Dictionary with agent behavior settings.
        """
        if not self._is_loaded:
            self.load_environment()

        return {
            "agent_timeout": int(self._config.get("AGENT_TIMEOUT", "300")),
            "max_shell_output_size": int(
                self._config.get("MAX_SHELL_OUTPUT_SIZE", "10000")
            ),
            "debug": self._config.get("DEBUG", "false").lower() == "true",
            "allowed_working_directory": self._config.get(
                "ALLOWED_WORKING_DIRECTORY", ""
            ),
            "log_level": self._config.get("LOG_LEVEL", "INFO"),
        }

    def get_config_value(self, key: str, default: str | None = None) -> str | None:
        """Get a specific configuration value.

        Args:
            key: Configuration key to retrieve.
            default: Default value if key is not found.

        Returns:
            Configuration value or default.
        """
        if not self._is_loaded:
            self.load_environment()

        return self._config.get(key, default)

    def create_env_file_if_missing(self) -> bool:
        """Create .env file from .env.example if it doesn't exist.

        Returns:
            True if .env file was created, False if it already exists.
        """
        if self.env_file.exists():
            return False

        env_example = self.project_root / ".env.example"
        if not env_example.exists():
            logger.warning("No .env.example file found to copy from")
            return False

        try:
            # Copy .env.example to .env
            with open(env_example) as src, open(self.env_file, "w") as dst:
                dst.write(src.read())
            logger.info(f"Created .env file from .env.example at {self.env_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to create .env file: {e}")
            return False

    def get_setup_instructions(self) -> str:
        """Get user-friendly setup instructions for missing configuration.

        Returns:
            Formatted string with setup instructions.
        """
        missing_keys = self.validate_configuration()
        if not missing_keys:
            return "✅ Configuration is valid and complete!"

        instructions = [
            "❌ Configuration setup required:",
            "",
        ]

        # Check if .env file exists
        if not self.env_file.exists():
            instructions.extend(
                [
                    "1. Create .env file:",
                    "   cp .env.example .env",
                    "",
                    "2. Edit .env file with your API configuration:",
                ]
            )
        else:
            instructions.extend(
                [
                    f"1. Edit your .env file at {self.env_file}:",
                ]
            )

        instructions.extend(
            [
                "",
                "Required configuration:",
            ]
        )

        for key in missing_keys:
            instructions.append(f"   - {key}")

        instructions.extend(
            [
                "",
                "Common API provider examples:",
                "",
                "OpenAI (recommended models):",
                "   OPENAI_API_KEY=sk-your_openai_key",
                "   OPENAI_BASE_URL=https://api.openai.com/v1",
                "   OPENAI_MODEL=gpt-4o-2024-11-20",
                "",
                "OpenRouter (use exact AutoGen model names):",
                "   OPENAI_API_KEY=sk-or-your_openrouter_key",
                "   OPENAI_BASE_URL=https://openrouter.ai/api/v1",
                "   OPENAI_MODEL=gpt-4o-2024-11-20",
                "",
                "Anthropic via OpenRouter:",
                "   OPENAI_API_KEY=sk-or-your_openrouter_key",
                "   OPENAI_BASE_URL=https://openrouter.ai/api/v1",
                "   OPENAI_MODEL=claude-3-5-sonnet-20241022",
                "",
                "GitHub Copilot (via LiteLLM):",
                "   OPENAI_API_KEY=your_github_token",
                "   OPENAI_BASE_URL=http://localhost:4000/v1",
                "   OPENAI_MODEL=github_copilot/claude-sonnet-4",
                "",
                "🎯 IMPORTANT: Use AutoGen's supported model names for best results!",
                "   The system will try to match your model name to AutoGen's built-in models.",
                "   Supported models include: gpt-4o-*, claude-*-*, gemini-*-*, o1-*, etc.",
                "",
                "❌ Manual model configuration is no longer needed:",
                "   MODEL_FAMILY, MODEL_VISION, etc. are automatically determined from model names.",
            ]
        )

        return "\n".join(instructions)

    def _is_valid_api_key_format(self, api_key: str) -> bool:
        """Check if API key has a valid format.

        Args:
            api_key: API key to validate.

        Returns:
            True if format appears valid.
        """
        if not api_key:
            return False

        # Common API key prefixes
        valid_prefixes = ["sk-", "sk-or-", "sk-ant-", "Bearer ", "sk-litellm-"]
        return any(api_key.startswith(prefix) for prefix in valid_prefixes)

    def _is_valid_url_format(self, url: str) -> bool:
        """Check if URL has a valid format.

        Args:
            url: URL to validate.

        Returns:
            True if format appears valid.
        """
        if not url:
            return False

        return url.startswith(("http://", "https://"))

    def _is_valid_numeric(self, value: str) -> bool:
        """Check if value can be converted to a number.

        Args:
            value: Value to check.

        Returns:
            True if value is numeric.
        """
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False

"""The system prompt, assembled from registered pieces.

Mount it and the agent loop builds its system prompt from the registry instead
of from ``AgentOptions.system``::

    await root.plugin(SystemPrompt, {"persona": "You are Ada, a research aide."})
    root.system_prompt.section(PromptSection("fs:guidance", 100, "Prefer relative paths."))

A plugin contributes a piece and never sees the whole; order is data, not
registration sequence; and every registration is scoped to the fiber that made
it, so unloading a plugin removes its contribution.
"""

from .interpolate import VARIABLE_NAME, PromptVariableError, interpolate
from .ordering import TOOL_ORDER_REST, ToolOrderError, order_tools
from .sections import PromptAssembly, PromptContext, PromptSection
from .service import (
    ASSEMBLE_WATERFALL,
    CONTEXT_SNAPSHOT_HEADER,
    HARNESS_IDENTITY_SECTION,
    PERSONA_ORDER,
    PERSONA_SECTION,
    PromptRegistrationError,
    SystemPrompt,
)

__all__ = [
    "SystemPrompt",
    "PromptSection",
    "PromptContext",
    "PromptAssembly",
    "PromptRegistrationError",
    "PromptVariableError",
    "ToolOrderError",
    "interpolate",
    "order_tools",
    "VARIABLE_NAME",
    "TOOL_ORDER_REST",
    "ASSEMBLE_WATERFALL",
    "HARNESS_IDENTITY_SECTION",
    "PERSONA_SECTION",
    "PERSONA_ORDER",
    "CONTEXT_SNAPSHOT_HEADER",
]

"""Runtime-only extensions keep the legacy tool catalogue unchanged."""
from services.agent.tools import TOOL_MAP as LEGACY_TOOLS
from . import import_document

TOOL_MAP = {**LEGACY_TOOLS, 'import_document': import_document}

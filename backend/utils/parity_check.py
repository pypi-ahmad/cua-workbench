"""Parity check utility to ensure alignment between tools_list.txt, ActionType, Tool Implementations, and Engine Capability Schema."""

import logging
import re
from typing import List, Set
from pathlib import Path

from backend.models import ActionType
from backend.agent.playwright_mcp_client import MCP_TOOL_HANDLERS
from backend.engine_capabilities import EngineCapabilities
from backend.tools.action_aliases import ACTION_ALIASES

logger = logging.getLogger(__name__)

TOOLS_LIST_PATH = Path(__file__).parent.parent.parent / "tools_list.txt"

def parse_tools_list() -> Set[str]:
    """Parse tools_list.txt to extract expected tool names."""
    if not TOOLS_LIST_PATH.exists():
        logger.error(f"tools_list.txt not found at {TOOLS_LIST_PATH}")
        return set()

    tools = set()
    content = TOOLS_LIST_PATH.read_text(encoding="utf-8")
    
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("* ") and not line.startswith("- "):
            continue
            
        text = line[2:].strip()
        parts = [p.strip() for p in text.split(" / ")]
        
        for part in parts:
            match_code = re.match(r"`([^`]+)`", part)
            if match_code:
                name = match_code.group(1).split("(")[0].strip()
                tools.add(name)
                continue
                
            match_func = re.match(r"([a-zA-Z0-9_]+)\(", part)
            if match_func:
                name = match_func.group(1).strip()
                tools.add(name)
                continue
                
            if re.match(r"^[a-z0-9_]+$", part):
                tools.add(part)

    return tools

def validate_tool_parity() -> bool:
    """
    Validate that:
    1. All tools in tools_list.txt are in ActionType.
    2. All ActionType members have a handler in MCP_TOOL_HANDLERS.
    3. All engine actions in engine_capabilities.json exist in ActionType.
    4. Prompt action lists align with the engine capability schema.
    """
    
    success = True
    expected_tools = parse_tools_list()
    action_type_values = {a.value for a in ActionType}
    
    logger.info("Found %d tools in tools_list.txt", len(expected_tools))
    
    # 1. tools_list.txt vs ActionType (resolve aliases first)
    alias_values = set(ACTION_ALIASES.keys())
    missing_in_enum = expected_tools - action_type_values - alias_values
    
    # Ignore known noise/implementation details that are not actions
    ignored_tools = {
        "xclip", "xsel", "wl-copy", "scrot", "imagemagick import", # external tools
        "--clearmodifiers", # flags
        'capabilities = ["core", "pdf", "vision"]', # config
    }
    
    actual_missing = missing_in_enum - ignored_tools
    
    if actual_missing:
        logger.warning("PARITY WARNING: Tools in text file but not in ActionType: %s", actual_missing)
        # Some may be handled as aliases in action_aliases.py
    
    # 2. ActionType vs MCP Handlers
    missing_handlers = []
    for action in ActionType:
        if action.value not in MCP_TOOL_HANDLERS:
            missing_handlers.append(action.value)
            
    if missing_handlers:
        logger.error("PARITY FAILURE: Missing MCP handlers for: %s", missing_handlers)
        success = False
    else:
        logger.info("PARITY SUCCESS: All ActionTypes are mapped in MCP_TOOL_HANDLERS.")

    # 3. Engine capability schema vs ActionType
    try:
        caps = EngineCapabilities()
        for engine_name in caps.engine_names:
            engine_actions = caps.get_engine_actions(engine_name)
            unknown = engine_actions - action_type_values
            if unknown:
                logger.warning(
                    "SCHEMA WARNING: engine_capabilities.json '%s' has actions "
                    "not in ActionType enum: %s", engine_name, sorted(unknown),
                )
        logger.info(
            "SCHEMA PARITY: Checked %d engines in engine_capabilities.json",
            len(caps.engine_names),
        )
    except Exception as e:
        logger.warning("Engine capability schema parity check failed: %s", e)

    # 4. Prompt action drift detection
    try:
        from backend.agent.prompts import validate_prompt_actions
        prompt_warnings = validate_prompt_actions()
        if prompt_warnings:
            for w in prompt_warnings:
                logger.warning("PROMPT DRIFT: %s", w)
        else:
            logger.info("PROMPT PARITY: All prompt action lists align with schema.")
    except Exception as e:
        logger.warning("Prompt parity check failed: %s", e)

    return success

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    validate_tool_parity()

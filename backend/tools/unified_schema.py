from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, ConfigDict, Field
import logging

from backend.models import ActionType, AgentAction
from backend.tools.action_aliases import resolve_action

logger = logging.getLogger(__name__)

class UnifiedAction(BaseModel):
    """Normalised action passed through the validation and routing pipeline."""

    model_config = ConfigDict(extra="ignore")

    action: str
    engine: str

    # Targeting
    target: Optional[str] = None
    selector: Optional[str] = None
    coordinates: Optional[List[int]] = None

    # Input
    text: Optional[str] = None

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)
    confidence: Optional[float] = None

    @property
    def canonical_action(self) -> Optional[ActionType]:
        """Return the matching ActionType enum member, or None."""
        try:
            return ActionType(self.action)
        except ValueError:
            return None

def normalize_action(action: Union[AgentAction, Dict[str, Any]], engine: str = "playwright_mcp") -> UnifiedAction:
    """Normalize a raw agent action into the UnifiedAction schema."""
    
    # 1. Convert to dictionary if needed
    if isinstance(action, AgentAction):
        # Handle Enum value extraction
        action_val = action.action.value if hasattr(action.action, "value") else str(action.action)
        raw_data = action.model_dump(exclude_none=True)
        raw_data["action"] = action_val
    elif isinstance(action, dict):
        raw_data = action.copy()
    else:
        raise ValueError(f"Invalid action type: {type(action)}")

    # 2. Resolve Action Alias
    original_action = raw_data.get("action", "")
    resolved_action = resolve_action(original_action)
    
    # 3. Initialize UnifiedAction with base data
    unified = UnifiedAction(
        action=resolved_action,
        engine=engine,
        text=raw_data.get("text"),
        coordinates=raw_data.get("coordinates"),
        metadata=raw_data.get("metadata", {}),
        confidence=raw_data.get("confidence")
    )

    # 4. Target Normalization rules
    # "target" -> "selector" for Playwright/MCP
    raw_target = raw_data.get("target") or raw_data.get("selector")
    
    if engine == "playwright_mcp":
        if raw_target:
            unified.selector = raw_target
            if not unified.target:
                unified.target = raw_target
    else:
        # For desktop, keep strictly as target if needed
        if raw_target and not unified.target:
            unified.target = raw_target

    # 5. Coordinate type enforcement
    if unified.coordinates:
        # Ensure it's a list of ints
        try:
            unified.coordinates = [int(c) for c in unified.coordinates]
        except (ValueError, TypeError):
             logger.warning("Invalid coordinates format: %s", unified.coordinates)
             unified.coordinates = None

    # 6. Text length cap (Phase 3 Requirement)
    if unified.text and len(unified.text) > 5000:
        logger.warning(f"Text too long ({len(unified.text)} chars), truncating to 5000")
        unified.text = unified.text[:5000]

    return unified

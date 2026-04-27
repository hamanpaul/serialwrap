"""sw_core.event_engine — pattern → spawn handler trigger engine (issue #37)."""
from __future__ import annotations

from .engine import EngineDeps, EventEngine
from .schema import Rule, validate_rule_dict

__all__ = ["EventEngine", "EngineDeps", "Rule", "validate_rule_dict"]

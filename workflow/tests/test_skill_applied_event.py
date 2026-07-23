"""Guard: skill.applied is a first-class RUN_EVENT_TYPE."""
from __future__ import annotations

from app.runtime.events import RUN_EVENT_TYPES


def test_skill_applied_in_run_event_types():
    assert "skill.applied" in RUN_EVENT_TYPES
    assert "skill.selected" in RUN_EVENT_TYPES
    assert "skill.failed" in RUN_EVENT_TYPES
    # Legacy aliases remain readable for historical events.
    assert "skill.started" in RUN_EVENT_TYPES
    assert "skill.completed" in RUN_EVENT_TYPES

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Set

from langchain_core.tools import StructuredTool

from app.config import settings
from app.tools import execute_skill, list_skills

logger = logging.getLogger(__name__)


def get_skill_tools(whitelist: Set[str]) -> List[StructuredTool]:
    tools: List[StructuredTool] = []
    if "list_skills" in whitelist:
        async def _list() -> str:
            return await list_skills()
        tools.append(StructuredTool.from_function(
            coroutine=_list,
            name="list_skills",
            description="List available skill metadata (lazy load, no full instructions)",
        ))
    if "execute_skill" in whitelist:
        async def _execute(skillName: str, task: str = "") -> str:
            return await execute_skill(skillName, task)
        tools.append(StructuredTool.from_function(
            coroutine=_execute,
            name="execute_skill",
            description="加载 Skill 指令，由当前 Agent 在后续 LLM round 中应用",
        ))
    return tools


def scan_skills_metadata() -> List[dict]:
    root = Path(settings.skills_path)
    if not root.is_dir():
        return []
    items = []
    for skill_dir in root.iterdir():
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            items.append({"name": skill_dir.name, "path": str(skill_file)})
    return items

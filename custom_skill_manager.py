"""Runtime custom skill manager for Servclaw.

Allows the agent to create, update, delete, and hot-reload Python tool scripts
at runtime. Custom skills live in workspace/skills/<tool_name>/ and are
available as LLM-callable tools immediately after creation.

Each skill folder contains:
  skill.py   — Python module; MUST define TOOL_SCHEMA (dict) and run(args: dict) -> dict
  schema.json — OpenAI function schema (extracted from TOOL_SCHEMA on create/update)
  SKILL.md   — Optional usage guide injected into the system prompt
"""

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

_VALID_NAME = re.compile(r'^[a-z][a-z0-9_]{1,49}$')
_RESERVED_NAMES = {"skill_create", "skill_update", "skill_delete", "skill_list", "skill_read"}


class CustomSkillManager:
    def __init__(self, workspace_dir: Path):
        self._skills_dir = workspace_dir / "skills"
        self._modules: dict = {}    # name → loaded module
        self._schemas: dict = {}    # name → OpenAI tool schema

    def load_all(self) -> None:
        """Scan workspace/skills/ and hot-load every skill.py found."""
        if not self._skills_dir.exists():
            return
        for skill_dir in self._skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "skill.py").exists():
                try:
                    self._load_module(skill_dir.name)
                    schema_path = skill_dir / "schema.json"
                    if schema_path.exists():
                        self._schemas[skill_dir.name] = json.loads(schema_path.read_text())
                except Exception as e:
                    print(f"[custom_skills] Failed to load {skill_dir.name}: {e}")

    def _load_module(self, name: str) -> None:
        path = self._skills_dir / name / "skill.py"
        spec = importlib.util.spec_from_file_location(f"_cskill_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_cskill_{name}"] = mod
        spec.loader.exec_module(mod)
        self._modules[name] = mod

    def create(self, name: str, code: str, guide: str = "") -> dict:
        if not _VALID_NAME.match(name):
            return {"error": "Invalid skill name (lowercase letters, digits, underscores; max 50 chars; must start with a letter)"}
        if name in _RESERVED_NAMES:
            return {"error": f"'{name}' is a reserved tool name"}
        if name in self._modules:
            return {"error": f"Skill '{name}' already exists. Use skill_update to modify it."}
        skill_dir = self._skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.py").write_text(code, encoding="utf-8")
        if guide:
            (skill_dir / "SKILL.md").write_text(guide, encoding="utf-8")
        # Load module to extract TOOL_SCHEMA
        try:
            self._load_module(name)
        except Exception as e:
            shutil.rmtree(skill_dir, ignore_errors=True)
            return {"error": f"Failed to load skill code: {e}"}
        mod = self._modules[name]
        if not hasattr(mod, "TOOL_SCHEMA"):
            shutil.rmtree(skill_dir, ignore_errors=True)
            self._modules.pop(name, None)
            return {"error": "skill.py must define TOOL_SCHEMA"}
        if not hasattr(mod, "run"):
            shutil.rmtree(skill_dir, ignore_errors=True)
            self._modules.pop(name, None)
            return {"error": "skill.py must define run(args: dict) -> dict"}
        schema = mod.TOOL_SCHEMA
        (skill_dir / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
        self._schemas[name] = schema
        return {"ok": True, "name": name}

    def update(self, name: str, code: str | None = None, guide: str | None = None) -> dict:
        skill_dir = self._skills_dir / name
        if not skill_dir.exists():
            return {"error": f"Skill '{name}' not found"}
        if code is not None:
            (skill_dir / "skill.py").write_text(code, encoding="utf-8")
        if guide is not None:
            (skill_dir / "SKILL.md").write_text(guide, encoding="utf-8")
        try:
            self._load_module(name)
        except Exception as e:
            return {"error": f"Failed to reload skill code: {e}"}
        mod = self._modules[name]
        if hasattr(mod, "TOOL_SCHEMA"):
            schema = mod.TOOL_SCHEMA
            (skill_dir / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
            self._schemas[name] = schema
        return {"ok": True, "name": name}

    def delete(self, name: str) -> dict:
        skill_dir = self._skills_dir / name
        if not skill_dir.exists():
            return {"error": f"Skill '{name}' not found"}
        shutil.rmtree(skill_dir)
        self._modules.pop(name, None)
        self._schemas.pop(name, None)
        return {"ok": True, "deleted": name}

    def execute(self, name: str, args: dict) -> dict:
        mod = self._modules.get(name)
        if not mod:
            return {"error": f"Skill '{name}' is not loaded"}
        try:
            return mod.run(args)
        except Exception as e:
            return {"error": f"Skill '{name}' execution error: {e}"}

    def read_skill(self, name: str) -> dict:
        skill_dir = self._skills_dir / name
        if not skill_dir.exists():
            return {"error": f"Skill '{name}' not found"}
        code = ""
        guide = ""
        code_path = skill_dir / "skill.py"
        guide_path = skill_dir / "SKILL.md"
        if code_path.exists():
            code = code_path.read_text(encoding="utf-8")
        if guide_path.exists():
            guide = guide_path.read_text(encoding="utf-8")
        return {"name": name, "code": code, "guide": guide}

    def get_schemas(self) -> list[dict]:
        return list(self._schemas.values())

    def get_guides(self) -> list[str]:
        guides = []
        for name in self._modules:
            path = self._skills_dir / name / "SKILL.md"
            if path.exists():
                guides.append(path.read_text(encoding="utf-8").strip())
        return guides

    def is_skill(self, name: str) -> bool:
        return name in self._modules

    def list_skills(self) -> list[dict]:
        result = []
        for name in self._modules:
            has_guide = (self._skills_dir / name / "SKILL.md").exists()
            schema = self._schemas.get(name, {})
            description = schema.get("function", {}).get("description", "") if schema else ""
            result.append({
                "name": name,
                "description": description[:120] if description else "",
                "has_guide": has_guide,
                "path": str(self._skills_dir / name),
            })
        return result

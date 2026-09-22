"""Skill catalog, loader, validator, and selection — blueprint "Skills as
versioned Markdown playbooks".

A skill teaches the agent how to apply tools within a domain. It must not
contain credentials or grant authority. Only *selected* skill text enters
model context, and selected versions are pinned in the context manifest so a
running job keeps working with the version it started with.

Selection is hybrid:
  1. Deterministic trigger match on named services and tool namespaces.
  2. Embedding search across skill descriptions.
  3. At most three candidates; the runtime verifies required namespaces.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from memory.embeddings import DeterministicEmbedder, cosine

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# A skill must never smuggle authority or secrets.
FORBIDDEN_PATTERNS = [
    re.compile(r"(?i)\b(ignore|override|bypass)\b.{0,40}\bpolic", re.IGNORECASE),
    re.compile(r"(?i)\b(system|developer)\s+prompt\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
]


@dataclass
class Skill:
    name: str
    version: str
    description: str
    triggers: list[str]
    required_namespaces: list[str]
    capabilities_used: list[str]
    risk_notes: list[str]
    max_prompt_tokens: int
    text: str  # SKILL.md body
    path: str

    def to_manifest(self) -> dict:
        return {"name": self.name, "version": self.version,
                "description": self.description}


class SkillValidationError(ValueError):
    pass


def _check_no_secrets(text: str, where: str) -> None:
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(text):
            raise SkillValidationError(
                f"{where}: forbidden pattern matched ({pat.pattern[:40]}…)")


def load_skill(skill_dir: str) -> Skill:
    """Load and validate one skill package. Raises SkillValidationError."""
    manifest_path = os.path.join(skill_dir, "manifest.yaml")
    text_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(manifest_path):
        raise SkillValidationError(f"{skill_dir}: missing manifest.yaml")
    if not os.path.isfile(text_path):
        raise SkillValidationError(f"{skill_dir}: missing SKILL.md")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh) or {}

    name = manifest.get("name", "")
    version = str(manifest.get("version", ""))
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise SkillValidationError(f"{skill_dir}: bad skill name {name!r}")
    if not SEMVER_RE.match(version):
        raise SkillValidationError(
            f"{skill_dir}: version must be semver, got {version!r}")

    required = manifest.get("required_namespaces", []) or []
    if not isinstance(required, list) or not required:
        raise SkillValidationError(
            f"{skill_dir}: required_namespaces must be a non-empty list")

    with open(text_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _check_no_secrets(text, f"skill {name}/SKILL.md")
    _check_no_secrets(yaml.safe_dump(manifest), f"skill {name}/manifest.yaml")

    max_tokens = int(manifest.get("max_prompt_tokens", 6000))
    return Skill(
        name=name, version=version,
        description=str(manifest.get("description", "")),
        triggers=[str(t) for t in (manifest.get("triggers", []) or [])],
        required_namespaces=[str(n) for n in required],
        capabilities_used=[str(c) for c in (manifest.get("capabilities_used", []) or [])],
        risk_notes=[str(r) for r in (manifest.get("risk_notes", []) or [])],
        max_prompt_tokens=max_tokens,
        text=text, path=skill_dir,
    )


class SkillCatalog:
    """Versioned skill catalog rooted at a skills directory."""

    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self._skills: dict[str, Skill] = {}
        self._embedder = DeterministicEmbedder()
        self.reload()

    def reload(self) -> None:
        self._skills = {}
        if not os.path.isdir(self.skills_dir):
            return
        for entry in sorted(os.listdir(self.skills_dir)):
            path = os.path.join(self.skills_dir, entry)
            if os.path.isdir(path):
                try:
                    skill = load_skill(path)
                except SkillValidationError:
                    continue  # invalid packages never enter the catalog
                self._skills[skill.name] = skill

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill:
        return self._skills[name]

    # -- selection ------------------------------------------------------
    def select(self, query: str, loaded_namespaces: set[str],
               *, limit: int = 3) -> list[Skill]:
        """Hybrid selection. Returns at most `limit` verified candidates."""
        q = query.lower()
        scored: dict[str, float] = {}
        # 1. deterministic trigger match
        for skill in self._skills.values():
            for trig in skill.triggers:
                if trig.lower() in q:
                    scored[skill.name] = scored.get(skill.name, 0.0) + 2.0
            for ns in skill.required_namespaces:
                if ns.lower() in q:
                    scored[skill.name] = scored.get(skill.name, 0.0) + 1.0
        # 2. embedding search across descriptions
        try:
            qv = self._embedder.embed([query])[0]
            for skill in self._skills.values():
                sv = self._embedder.embed(
                    [f"{skill.name} {skill.description} {' '.join(skill.triggers)}"])[0]
                sim = cosine(qv, sv)
                if sim > 0.15:
                    scored[skill.name] = scored.get(skill.name, 0.0) + sim
        except Exception:
            pass  # embedding failure must not break deterministic selection
        ranked = sorted(scored, key=lambda n: scored[n], reverse=True)[:limit]
        # 3. the runtime verifies required namespaces before admission
        return [self._skills[n] for n in ranked
                if set(self._skills[n].required_namespaces) <= set(loaded_namespaces)]

    # -- context injection ----------------------------------------------
    def render_context(self, skills: list[Skill]) -> tuple[str, dict]:
        """Render ONLY the selected skills' text; return (text, pinned manifest).

        The pinned manifest records exact versions so the running job keeps
        the version it started with even if a skill is promoted mid-run.
        """
        parts = []
        pinned = {}
        for skill in skills:
            body = skill.text
            # crude token guard: ~4 chars per token
            char_cap = skill.max_prompt_tokens * 4
            if len(body) > char_cap:
                body = body[:char_cap] + "\n…[truncated to max_prompt_tokens]"
            parts.append(f"<!-- skill:{skill.name}@{skill.version} -->\n{body}")
            pinned[skill.name] = skill.version
        return "\n\n".join(parts), pinned

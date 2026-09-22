"""
People and groups (Layer 4): relationship pages with a conservative index.

One Markdown page per entity under <root>/pages/<slug>.md, plus an INDEX.md
with names, aliases, and relationship summaries. Entity resolution is
deliberately conservative: an exact normalized-name or alias match resolves;
anything else returns candidates and never merges. In particular, two people
are never merged on a shared first name alone.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from .records import new_id, utcnow


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "person"


class PersonPage:
    def __init__(self, data: dict):
        self.data = data

    @property
    def person_id(self) -> str:
        return self.data["person_id"]

    @property
    def name(self) -> str:
        return self.data["name"]


class PeopleIndex:
    def __init__(self, root: str):
        self.root = root
        self._pages_dir = os.path.join(root, "pages")
        os.makedirs(self._pages_dir, exist_ok=True)
        self._index_path = os.path.join(root, "people.json")
        self._people: dict[str, dict] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self._index_path):
            with open(self._index_path, "r", encoding="utf-8") as fh:
                try:
                    self._people = json.load(fh)
                except json.JSONDecodeError:
                    self._people = {}

    def _save(self) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._people, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._index_path)
        self._write_index_md()

    # -- resolution ----------------------------------------------------------
    def resolve(self, name: str) -> dict:
        """Conservative resolution.

        Returns {"status": "resolved", "person": ...} on exact normalized-name
        or alias match; {"status": "ambiguous", "candidates": [...]} when
        several pages share a first-name token; {"status": "not_found"}.
        Never merges entities.
        """
        norm = _normalize(name)
        exact = [p for p in self._people.values()
                 if _normalize(p["name"]) == norm or norm in {_normalize(a) for a in p.get("aliases", [])}]
        if len(exact) == 1:
            return {"status": "resolved", "person": exact[0]}
        if len(exact) > 1:
            return {"status": "ambiguous",
                    "candidates": [{"person_id": p["person_id"], "name": p["name"]} for p in exact]}
        first = norm.split(" ")[0]
        partial = [p for p in self._people.values()
                   if _normalize(p["name"]).split(" ")[0] == first]
        if partial:
            return {"status": "ambiguous",
                    "candidates": [{"person_id": p["person_id"], "name": p["name"]} for p in partial]}
        return {"status": "not_found"}

    # -- writes --------------------------------------------------------------
    def create_person(self, name: str, relationship: str = "") -> dict:
        """Always create a new page. Use after ambiguity was resolved with
        the user (e.g. 'yes, a different Rohan') — never auto-merge."""
        person = {"person_id": new_id("person"), "name": name.strip(),
                  "aliases": [], "relationship": relationship,
                  "facts": [], "interactions": [], "created_at": utcnow()}
        self._people[person["person_id"]] = person
        self._write_page(person)
        self._save()
        return person

    def get_or_create(self, name: str, relationship: str = "") -> dict:
        res = self.resolve(name)
        if res["status"] == "resolved":
            return res["person"]
        if res["status"] == "ambiguous":
            raise AmbiguousPersonError(name, res["candidates"])
        return self.create_person(name, relationship)

    def add_fact(self, name: str, fact: str, source_ref: str = "",
                 relationship: str = "") -> dict:
        person = self.get_or_create(name, relationship)
        entry = {"fact": fact.strip(), "source_ref": source_ref, "added_at": utcnow()}
        if not any(f["fact"] == entry["fact"] for f in person["facts"]):
            person["facts"].append(entry)
        self._write_page(person)
        self._save()
        return person

    def add_interaction(self, name: str, date: str, summary: str,
                        event_ref: str = "") -> dict:
        person = self.get_or_create(name)
        person["interactions"].append(
            {"date": date, "summary": summary.strip(), "event_ref": event_ref})
        self._write_page(person)
        self._save()
        return person

    def add_alias(self, person_id: str, alias: str) -> None:
        person = self._people[person_id]
        if _normalize(alias) not in {_normalize(a) for a in person["aliases"]}:
            person["aliases"].append(alias.strip())
        self._write_page(person)
        self._save()

    def remove_person(self, person_id: str) -> bool:
        """Forgetting support: drop the page and index entry entirely."""
        person = self._people.pop(person_id, None)
        if person is None:
            return False
        path = os.path.join(self._pages_dir, _slug(person["name"]) + ".md")
        if os.path.exists(path):
            os.remove(path)
        self._save()
        return True

    def list_people(self) -> list[dict]:
        return [{"person_id": p["person_id"], "name": p["name"],
                 "relationship": p.get("relationship", ""),
                 "fact_count": len(p.get("facts", []))}
                for p in self._people.values()]

    # -- projections ---------------------------------------------------------
    def _write_page(self, person: dict) -> None:
        path = os.path.join(self._pages_dir, _slug(person["name"]) + ".md")
        lines = [f"# {person['name']}", ""]
        if person.get("relationship"):
            lines += ["## Relationship", f"- {person['relationship']}", ""]
        if person.get("aliases"):
            lines += ["## Aliases", "".join(f"- {a}\n" for a in person["aliases"]), ""]
        if person.get("facts"):
            lines += ["## Relevant context"]
            lines += [f"- {f['fact']}" + (f" [{f['source_ref']}]" if f.get("source_ref") else "")
                      for f in person["facts"]]
            lines.append("")
        if person.get("interactions"):
            lines += ["## Recent interactions"]
            lines += [f"- {i['date']}: {i['summary']}" + (f" [{i['event_ref']}]" if i.get("event_ref") else "")
                      for i in person["interactions"]]
            lines.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")

    def _write_index_md(self) -> None:
        path = os.path.join(self.root, "INDEX.md")
        lines = ["# People Index", ""]
        for p in sorted(self._people.values(), key=lambda x: x["name"].lower()):
            rel = f" — {p['relationship']}" if p.get("relationship") else ""
            lines.append(f"- **{p['name']}** (`{p['person_id']}`){rel}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


class AmbiguousPersonError(Exception):
    def __init__(self, name: str, candidates: list[dict]):
        self.name = name
        self.candidates = candidates
        super().__init__(f"ambiguous person reference {name!r}: "
                         + ", ".join(c["name"] for c in candidates))

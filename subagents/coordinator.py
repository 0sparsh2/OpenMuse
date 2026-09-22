"""Coordinator — fan-out/fan-in, pipeline joins, and typed parent synthesis.

Delegation patterns (blueprint "Subagents and delegation"):
  - Fan-out/fan-in: independent tasks; the parent synthesizes.
  - Pipeline: gather -> normalize -> analyze -> draft; each stage consumes a
    typed artifact from the previous stage via context_refs.
  - Critic and Specialist are expressible as fan-out with join policies.

Join semantics:
  - all:    every child must complete; one terminal failure invalidates the
            objective and is reported (the parent decides what to do).
  - any:    the first valid result wins; the rest are cancelled.
  - quorum: N independently valid results are required.

The parent owns synthesis and must restate material child findings; opaque
"see child" responses are not acceptable. The synthesis is typed and sourced:
every claim carries evidence_refs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.seams import DelegationRequest

from subagents.models import ChildStatus, DelegationRecord, JoinPolicy
from subagents.runner import SubagentRunner


@dataclass
class SpawnSpec:
    """One child in a fan-out or pipeline."""
    task: str
    allowed_namespaces: list[str]
    output_contract: dict = field(default_factory=dict)
    context_refs: list[dict] = field(default_factory=list)
    budget_carve: dict = field(default_factory=dict)
    explicit_writes: list[str] = field(default_factory=list)
    join_policy: str = JoinPolicy.ALL
    artifact_id: str = ""  # set by the coordinator on completion


@dataclass
class SynthesisResult:
    status: str  # synthesized | incomplete | failed
    synthesis: dict  # typed per the synthesis contract, with evidence_refs
    children: list[dict]  # per-child summaries
    notes: str = ""


def _to_request(parent_run_id: str, spec: SpawnSpec, max_depth: int) -> DelegationRequest:
    return DelegationRequest(
        task=spec.task,
        allowed_namespaces=list(spec.allowed_namespaces),
        budget_carve=dict(spec.budget_carve),
        max_depth=max_depth,
        parent_run_id=parent_run_id,
        output_contract=dict(spec.output_contract),
        context_refs=list(spec.context_refs),
        join_policy=spec.join_policy,
        explicit_writes=list(spec.explicit_writes),
    )


def fan_out(runner: SubagentRunner, parent_run_id: str,
            specs: list[SpawnSpec], *, max_depth: int = 2,
            defer: bool = False) -> list[str]:
    """Spawn one child per spec. Returns delegation ids in spec order."""
    ids = []
    for spec in specs:
        ids.append(runner.spawn(_to_request(parent_run_id, spec, max_depth), defer=defer))
    return ids


def _valid(rec: DelegationRecord) -> bool:
    return (rec.result is not None
            and rec.result.status == ChildStatus.COMPLETED)


def fan_in(runner: SubagentRunner, delegation_ids: list[str], *,
           join_policy: str = JoinPolicy.ALL,
           quorum_n: int = 2,
           synthesize: Optional[Callable[[list[DelegationRecord]], dict]] = None,
           ) -> SynthesisResult:
    """Collect child results per the join policy and run parent synthesis."""
    recs = [runner._delegations[i] for i in delegation_ids]  # coordinator is trusted
    children = [r.to_summary() for r in recs]
    valids = [r for r in recs if _valid(r)]

    if join_policy == JoinPolicy.ANY:
        if not valids:
            return SynthesisResult(status="failed", synthesis={},
                                   children=children,
                                   notes="no child produced a valid result")
        winner = valids[0]
        for r in recs:
            if r.delegation_id != winner.delegation_id and not r.closed:
                runner.close(r.delegation_id)
        chosen = [winner]
    elif join_policy == JoinPolicy.QUORUM:
        if len(valids) < quorum_n:
            return SynthesisResult(
                status="incomplete", synthesis={}, children=children,
                notes=f"quorum not met: {len(valids)}/{quorum_n} valid results")
        chosen = valids[:quorum_n]
    else:  # ALL
        bad = [r for r in recs if not _valid(r)]
        if bad:
            return SynthesisResult(
                status="incomplete", synthesis={}, children=children,
                notes="terminal failure in: " + ", ".join(
                    f"{r.delegation_id} ({r.status})" for r in bad))
        chosen = recs

    synthesis = synthesize(chosen) if synthesize else default_synthesis(chosen)
    return SynthesisResult(status="synthesized", synthesis=synthesis,
                           children=children)


def default_synthesis(recs: list[DelegationRecord]) -> dict:
    """Typed, sourced synthesis: restate findings, never 'see child'."""
    findings = []
    evidence_refs: list[str] = []
    for r in recs:
        assert r.result is not None
        findings.append({
            "delegation_id": r.delegation_id,
            "objective": r.objective,
            "output": r.result.output,
        })
        evidence_refs.append(r.delegation_id)
        evidence_refs.extend(r.result.evidence_refs)
    return {"findings": findings, "evidence_refs": sorted(set(evidence_refs))}


def pipeline(runner: SubagentRunner, parent_run_id: str,
             stages: list[SpawnSpec], *, max_depth: int = 2) -> SynthesisResult:
    """Run stages in order; each stage consumes the previous stage's typed
    output as a context ref (child artifact reference)."""
    recs: list[DelegationRecord] = []
    prior_artifact: Optional[dict] = None
    for i, spec in enumerate(stages):
        refs = list(spec.context_refs)
        if prior_artifact is not None:
            refs.append({"artifact_id": prior_artifact["artifact_id"],
                         "stage": i - 1,
                         "output": prior_artifact["output"]})
        spec = SpawnSpec(task=spec.task,
                         allowed_namespaces=spec.allowed_namespaces,
                         output_contract=spec.output_contract,
                         context_refs=refs, budget_carve=spec.budget_carve,
                         explicit_writes=spec.explicit_writes,
                         join_policy=spec.join_policy)
        did = runner.spawn(_to_request(parent_run_id, spec, max_depth))
        rec = runner._delegations[did]
        recs.append(rec)
        if not _valid(rec):
            return SynthesisResult(
                status="incomplete",
                synthesis=default_synthesis([r for r in recs if _valid(r)]),
                children=[r.to_summary() for r in recs],
                notes=f"pipeline halted at stage {i}: {rec.status}")
        assert rec.result is not None
        prior_artifact = {"artifact_id": f"art_{uuid.uuid4().hex[:12]}",
                          "stage": i, "output": rec.result.output}
    last = recs[-1]
    assert last.result is not None
    return SynthesisResult(
        status="synthesized",
        synthesis={"pipeline_output": last.result.output,
                   "stages": len(recs),
                   "evidence_refs": [r.delegation_id for r in recs]},
        children=[r.to_summary() for r in recs])

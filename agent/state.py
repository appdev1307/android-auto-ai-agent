from typing import TypedDict, Annotated, List, Dict, Optional, Any
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

from agent.diagnosis import Diagnosis


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    bug_report: str
    logcat_snippet: Optional[str]
    aosp_root: Optional[str]
    tenant: Optional[Dict[str, str]]
    task_type: str
    evidence: List[Dict[str, Any]]      # retrieval hits actually gathered this run
    retrieved: List[Dict[str, Any]]     # deterministic seed-retrieval hits (localization floor)
    diagnosis: Diagnosis                # committed single source of truth
    candidate_files: List[str]
    root_cause: Optional[str]
    patches: List[Dict[str, Any]]
    unit_tests: List[Dict[str, Any]]
    status: str
    needs_human_review: bool
    iterations: int
    relocalize_count: int               # capped re-localization passes after specialist reject
    specialist_notes: list
    specialist_rounds: int              # ReConcile-style debate rounds actually run
    specialist_consensus: Dict[str, Any]  # majority verdict + force_human_review

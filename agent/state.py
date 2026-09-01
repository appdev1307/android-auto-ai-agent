from typing import TypedDict, Annotated, List, Dict, Optional, Any
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    bug_report: str
    logcat_snippet: Optional[str]
    aosp_root: Optional[str]
    tenant: Optional[Dict[str, str]]
    task_type: str
    evidence: List[Dict[str, Any]]      # retrieval hits
    candidate_files: List[str]
    root_cause: Optional[str]
    patches: List[Dict[str, Any]]
    unit_tests: List[Dict[str, Any]]
    status: str
    needs_human_review: bool
    iterations: int
    specialist_notes: list

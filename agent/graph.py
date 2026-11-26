from langgraph.graph import StateGraph, START, END
from agent.state import AgentState
from agent.nodes import (init_retriever, seed_retrieval, agent_reason, should_continue,
                         tool_node, commit_diagnosis, finalize, specialists,
                         relocalize, should_relocalize, after_relocalize, propose_criteria)


def build_graph(gate: bool = False, checkpointer=None):
    """gate=True stops before `finalize` (after diagnosis + specialists + [XK-n]) so a
    human approves / replaces the criteria or rejects the run; needs a checkpointer
    (agent/checkpoint.open_saver) so `--resume` can continue in a new process.
    gate=False (eval, batch) runs straight through, same nodes."""
    if gate and checkpointer is None:
        raise ValueError("gate=True needs a checkpointer")
    g = StateGraph(AgentState)
    g.add_node("init_retriever", init_retriever)
    g.add_node("seed_retrieval", seed_retrieval)  # deterministic first retrieval (localization floor)
    g.add_node("agent", agent_reason)
    g.add_node("tools", tool_node)
    g.add_node("commit", commit_diagnosis)   # free-text -> committed, validated facts
    g.add_node("specialists", specialists)   # per-layer multi-agent pass
    g.add_node("relocalize", relocalize)     # specialists rejected target -> retry on their hints
    g.add_node("criteria", propose_criteria)  # [XK-n] for the settled diagnosis
    g.add_node("finalize", finalize)

    g.add_edge(START, "init_retriever")
    g.add_edge("init_retriever", "seed_retrieval")
    g.add_edge("seed_retrieval", "agent")
    g.add_conditional_edges("agent", should_continue,
                            {"tools": "tools", "commit": "commit"})
    g.add_edge("tools", "agent")             # ReAct loop
    g.add_edge("commit", "specialists")      # commit diagnosis -> validate per layer
    # specialists -> re-localize once if they unanimously reject the target, else criteria
    g.add_conditional_edges("specialists", should_relocalize,
                            {"relocalize": "relocalize", "finalize": "criteria"})
    # re-seeded -> re-commit -> specialists (capped); nothing new -> criteria
    g.add_conditional_edges("relocalize", after_relocalize,
                            {"commit": "commit", "finalize": "criteria"})
    g.add_edge("criteria", "finalize")       # HUMAN GATE sits here (interrupt_before)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer,
                     interrupt_before=["finalize"] if gate else None)

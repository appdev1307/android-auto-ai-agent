from langgraph.graph import StateGraph, START, END
from agent.state import AgentState
from agent.nodes import (init_retriever, seed_retrieval, agent_reason, should_continue,
                         tool_node, commit_diagnosis, finalize, specialists,
                         relocalize, should_relocalize)


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("init_retriever", init_retriever)
    g.add_node("seed_retrieval", seed_retrieval)  # deterministic first retrieval (localization floor)
    g.add_node("agent", agent_reason)
    g.add_node("tools", tool_node)
    g.add_node("commit", commit_diagnosis)   # free-text -> committed, validated facts
    g.add_node("specialists", specialists)   # per-layer multi-agent pass
    g.add_node("relocalize", relocalize)     # specialists rejected target -> retry on their hints
    g.add_node("finalize", finalize)

    g.add_edge(START, "init_retriever")
    g.add_edge("init_retriever", "seed_retrieval")
    g.add_edge("seed_retrieval", "agent")
    g.add_conditional_edges("agent", should_continue,
                            {"tools": "tools", "commit": "commit"})
    g.add_edge("tools", "agent")             # ReAct loop
    g.add_edge("commit", "specialists")      # commit diagnosis -> validate per layer
    # specialists -> re-localize once if they unanimously reject the target, else finalize
    g.add_conditional_edges("specialists", should_relocalize,
                            {"relocalize": "relocalize", "finalize": "finalize"})
    g.add_edge("relocalize", "commit")       # re-seeded -> re-commit -> specialists (capped)
    g.add_edge("finalize", END)
    return g.compile()
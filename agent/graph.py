from langgraph.graph import StateGraph, START, END
from agent.state import AgentState
from agent.nodes import (init_retriever, agent_reason, should_continue,
                         tool_node, finalize, specialists)


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("init_retriever", init_retriever)
    g.add_node("agent", agent_reason)
    g.add_node("tools", tool_node)
    g.add_node("specialists", specialists)   # per-layer multi-agent pass
    g.add_node("finalize", finalize)

    g.add_edge(START, "init_retriever")
    g.add_edge("init_retriever", "agent")
    g.add_conditional_edges("agent", should_continue,
                            {"tools": "tools", "specialists": "specialists"})
    g.add_edge("tools", "agent")             # ReAct loop
    g.add_edge("specialists", "finalize")    # specialists -> aggregate
    g.add_edge("finalize", END)
    return g.compile()

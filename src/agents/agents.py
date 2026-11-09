"""Transitional SCAT agent registry; Hermes integration is developed separately."""

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from langgraph.pregel import Pregel

from agents.lazy_agent import LazyLoadingAgent
from agents.scat_agent import scat_agent
from schema import AgentInfo

DEFAULT_AGENT = "scat-flight-agent"
AgentGraph = CompiledStateGraph | Pregel
AgentGraphLike = CompiledStateGraph | Pregel | LazyLoadingAgent


@dataclass
class Agent:
    description: str
    graph_like: AgentGraphLike
    expose: bool = True


agents: dict[str, Agent] = {
    DEFAULT_AGENT: Agent(
        description="SCAT/UAV business queries, model tasks and analysis results.",
        graph_like=scat_agent,
    )
}


async def load_agent(agent_id: str) -> None:
    graph_like = agents[agent_id].graph_like
    if isinstance(graph_like, LazyLoadingAgent):
        await graph_like.load()


def get_agent(agent_id: str) -> AgentGraph:
    graph_like = agents[agent_id].graph_like
    if isinstance(graph_like, LazyLoadingAgent):
        return graph_like.get_graph()
    return graph_like


def get_public_agent(agent_id: str) -> AgentGraph:
    if not agents[agent_id].expose:
        raise KeyError(agent_id)
    return get_agent(agent_id)


def get_all_agent_info() -> list[AgentInfo]:
    return [
        AgentInfo(key=key, description=value.description)
        for key, value in agents.items()
        if value.expose
    ]

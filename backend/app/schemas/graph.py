from uuid import UUID

from pydantic import BaseModel


class GraphNodeResponse(BaseModel):
    id: str
    type: str
    name: str
    path: str | None


class GraphEdgeResponse(BaseModel):
    source: str
    target: str
    type: str


class GraphResponse(BaseModel):
    snapshot_id: UUID
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    truncated: bool

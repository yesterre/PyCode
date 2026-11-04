from uuid import UUID

from pydantic import BaseModel, Field


class GraphNodeResponse(BaseModel):
    id: str
    type: str
    name: str
    path: str | None
    symbol_key: str | None = None
    parent_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None


class GraphEdgeResponse(BaseModel):
    source: str
    target: str
    type: str
    site_id: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    resolution: str | None = None
    basis: str | None = None


class GraphResponse(BaseModel):
    snapshot_id: UUID
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    truncated: bool
    graph_schema_version: int = 1
    graph_capabilities: list[str] = Field(default_factory=list)
    graph_warnings: list[str] = Field(default_factory=list)
    call_sites: list[dict] = Field(default_factory=list)
    call_sites_omitted: int = 0

[CmdletBinding()]
param(
    [string]$Neo4jContainer = "ott-neo4j",
    [string]$Neo4jUser = "neo4j",
    [string]$Neo4jPassword = "ott_detection_2025",
    [string]$Neo4jDatabase = "neo4j"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Cypher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Query,
        [string]$Description = "Cypher query",
        [switch]$SuppressOutput
    )

    Write-Step $Description
    if ($SuppressOutput.IsPresent) {
        docker exec $Neo4jContainer cypher-shell -d $Neo4jDatabase -u $Neo4jUser -p $Neo4jPassword "$Query" | Out-Null
    }
    else {
        docker exec $Neo4jContainer cypher-shell -d $Neo4jDatabase -u $Neo4jUser -p $Neo4jPassword "$Query"
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to execute Neo4j query: $Description"
    }
}

function Show-LegacyCounts {
    Invoke-Cypher -Description "Current legacy artifact counts" -Query @"
MATCH (v:Video)
WITH count(v) AS video_nodes
OPTIONAL MATCH ()-[fv:FOR_VIDEO]->()
WITH video_nodes, count(fv) AS for_video_rels
OPTIONAL MATCH ()-[po:PART_OF]->()
WITH video_nodes, for_video_rels, count(po) AS part_of_rels
OPTIONAL MATCH ()-[fc:FOR_CONTENT]->()
RETURN video_nodes, for_video_rels, part_of_rels, count(fc) AS for_content_rels;
"@

    Invoke-Cypher -Description "Current canonical relationship counts" -Query @"
OPTIONAL MATCH ()-[tc:TARGETS_CONTENT]->()
WITH count(tc) AS targets_content_rels
OPTIONAL MATCH ()-[bs:BELONGS_TO]->()
RETURN targets_content_rels, count(bs) AS belongs_to_rels;
"@
}

Write-Host "[Neo4j Legacy Schema Normalize] Start" -ForegroundColor Green
Write-Host "Neo4j container: $Neo4jContainer"

Invoke-Cypher -Description "Connectivity check" -Query "RETURN 1 AS ok;"
Show-LegacyCounts

Invoke-Cypher -Description "Migrate FOR_CONTENT -> TARGETS_CONTENT" -SuppressOutput -Query @"
MATCH (r:Request)-[rel:FOR_CONTENT]->(c:Content)
MERGE (r)-[:TARGETS_CONTENT]->(c)
DELETE rel;
"@

Invoke-Cypher -Description "Migrate FOR_VIDEO -> TARGETS_CONTENT with Content nodes" -SuppressOutput -Query @"
MATCH (r:Request)-[rel:FOR_VIDEO]->(v:Video)
WITH r, rel, v, coalesce(v.video_id, v.content_id, v.filename, 'video_' + toString(id(v))) AS cid
MERGE (c:Content {content_id: cid})
ON CREATE SET
    c.title = coalesce(v.title, cid),
    c.type = coalesce(v.type, 'HLS_STREAM')
MERGE (r)-[:TARGETS_CONTENT]->(c)
DELETE rel;
"@

Invoke-Cypher -Description "Migrate Segment PART_OF -> BELONGS_TO with Content nodes" -SuppressOutput -Query @"
MATCH (s:Segment)-[rel:PART_OF]->(v:Video)
WITH s, rel, v, coalesce(v.video_id, v.content_id, v.filename, 'video_' + toString(id(v))) AS cid
MERGE (c:Content {content_id: cid})
ON CREATE SET
    c.title = coalesce(v.title, cid),
    c.type = coalesce(v.type, 'HLS_STREAM')
MERGE (s)-[:BELONGS_TO]->(c)
DELETE rel;
"@

Invoke-Cypher -Description "Backfill TARGETS_CONTENT from FOR_SEGMENT/BELONGS_TO" -SuppressOutput -Query @"
MATCH (r:Request)-[:FOR_SEGMENT]->(:Segment)-[:BELONGS_TO]->(c:Content)
MERGE (r)-[:TARGETS_CONTENT]->(c);
"@

Invoke-Cypher -Description "Delete remaining Video nodes" -SuppressOutput -Query @"
MATCH (v:Video)
DETACH DELETE v;
"@

Write-Step "Post-migration verification"
Show-LegacyCounts

Write-Host "`n[Neo4j Legacy Schema Normalize] Completed" -ForegroundColor Green

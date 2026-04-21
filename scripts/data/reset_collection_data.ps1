[CmdletBinding()]
param(
    [string]$ElasticsearchUrl = "http://localhost:9200",
    [string]$ElasticsearchPattern = "scrubber-nginx-*,filebeat-*,.ds-scrubber-nginx-*,.ds-filebeat-*",
    [string]$Neo4jContainer = "ott-neo4j",
    [string]$Neo4jUser = "neo4j",
    [string]$Neo4jPassword = "ott_detection_2025",
    [string]$Neo4jDatabase = "neo4j",
    [string]$PostgresContainer = "ott-postgres",
    [string]$PostgresUser = "ott_user",
    [string]$PostgresDb = "ott_auth",
    [string]$ComposeFile = "docker-compose.lab.yml",
    [switch]$ClearPostgresActivity,
    [switch]$ResetFilebeatOffsets
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Compose {
    param([string[]]$Args)

    docker compose @Args
    if ($LASTEXITCODE -eq 0) {
        return
    }

    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        docker-compose @Args
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    throw "docker compose command failed: $($Args -join ' ')"
}

function Delete-ElasticsearchLogs {
    Write-Step "Delete Elasticsearch indices/data streams"
    $uri = "$ElasticsearchUrl/$ElasticsearchPattern?ignore_unavailable=true&expand_wildcards=all"

    try {
        $response = Invoke-WebRequest -Method Delete -Uri $uri -UseBasicParsing -TimeoutSec 30
        Write-Host "Elasticsearch delete response: $($response.StatusCode)"
    }
    catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -eq 404) {
            Write-Host "No matching indices found (already clean)." -ForegroundColor Yellow
            return
        }
        throw
    }
}

function Reset-Neo4jGraph {
    Write-Step "Delete all Neo4j graph nodes/relationships"
    docker exec $Neo4jContainer cypher-shell -d $Neo4jDatabase -u $Neo4jUser -p $Neo4jPassword "MATCH (n) DETACH DELETE n;"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to clear Neo4j graph"
    }
}

function Reset-PostgresActivity {
    Write-Step "Truncate PostgreSQL activity tables"
    $sql = "TRUNCATE TABLE watch_history, sessions, audit_logs RESTART IDENTITY CASCADE;"
    docker exec $PostgresContainer psql -U $PostgresUser -d $PostgresDb -v ON_ERROR_STOP=1 -c $sql
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to truncate PostgreSQL tables"
    }
}

function Reset-FilebeatOffsets {
    Write-Step "Reset local filebeat offsets"
    Invoke-Compose -Args @("-f", $ComposeFile, "stop", "filebeat")
    Invoke-Compose -Args @("-f", $ComposeFile, "rm", "-f", "filebeat")

    $volumes = docker volume ls --format "{{.Name}}" | Where-Object { $_ -match "filebeat_data$" }
    foreach ($volume in $volumes) {
        Write-Host "Removing volume: $volume"
        docker volume rm $volume | Out-Null
    }

    Invoke-Compose -Args @("-f", $ComposeFile, "up", "-d", "filebeat")
}

Write-Host "[Collection Reset] Start" -ForegroundColor Green
Write-Host "Elasticsearch URL: $ElasticsearchUrl"
Write-Host "Neo4j container: $Neo4jContainer"

Delete-ElasticsearchLogs
Reset-Neo4jGraph

if ($ClearPostgresActivity.IsPresent) {
    Reset-PostgresActivity
}

if ($ResetFilebeatOffsets.IsPresent) {
    Reset-FilebeatOffsets
}

Write-Host "`n[Collection Reset] Completed" -ForegroundColor Green
Write-Host "Tip: start scenario run with run_id/scenario_id/dataset_label metadata." -ForegroundColor DarkGray

const crypto = require('crypto')

function firstForwardedIp(value) {
  if (!value) return ''
  return String(value).split(',')[0].trim().replace(/^::ffff:/, '')
}

function requestClientIp(req) {
  return firstForwardedIp(
    req.get('x-real-ip')
      || req.get('x-forwarded-for')
      || req.ip
      || req.socket?.remoteAddress
      || ''
  )
}

function safeTag(value, maxLength = 64) {
  if (value === undefined || value === null) return ''
  return String(value)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '')
    .slice(0, maxLength)
}

function metadataFromRequest(req) {
  const read = (name, aliases = []) => {
    for (const key of [name, ...aliases]) {
      const value = req.body?.[key] ?? req.query?.[key]
      if (value !== undefined && value !== null && String(value).trim()) {
        return value
      }
    }
    return ''
  }

  return {
    runId: safeTag(read('run_id'), 48),
    scenarioId: safeTag(read('scenario_id'), 24).toUpperCase(),
    label: safeTag(read('label', ['scenario_label', 'dataset_label']), 48),
    datasetLabel: safeTag(read('dataset_label', ['label']), 48),
    deviceId: safeTag(read('device_id', ['deviceId']), 64),
    logicalClientId: safeTag(
      read('logical_client_id', ['logicalClientId', 'client_id']),
      64
    ),
    physicalHostId: safeTag(read('physical_host_id', ['physicalHostId']), 64),
    networkProfileId: safeTag(
      read('network_profile_id', ['networkProfileId']),
      32
    ).toUpperCase(),
  }
}

function tokenGraphId(token) {
  if (!token) return ''
  const digest = crypto.createHash('sha256').update(String(token)).digest('hex')
  return `cdn_${digest.slice(0, 24)}`
}

async function emitApiEvent(req, event) {
  const esClient = req.app.locals.esClient
  if (!esClient) return false

  const now = new Date()
  const metadata = metadataFromRequest(req)
  const tokenPayload = event.tokenPayload || {}
  const issuedAt = tokenPayload.iat
    ? new Date(Number(tokenPayload.iat) * 1000).toISOString()
    : null
  const expiresAt = tokenPayload.exp
    ? new Date(Number(tokenPayload.exp) * 1000).toISOString()
    : null
  const tokenTtlSec = tokenPayload.iat && tokenPayload.exp
    ? Math.max(0, Number(tokenPayload.exp) - Number(tokenPayload.iat))
    : 0

  const query = new URLSearchParams()
  if (event.contentId) query.set('content_id', String(event.contentId))
  if (metadata.runId) query.set('run_id', metadata.runId)
  if (metadata.scenarioId) query.set('scenario_id', metadata.scenarioId)
  if (metadata.label) query.set('label', metadata.label)
  if (metadata.datasetLabel) query.set('dataset_label', metadata.datasetLabel)
  if (metadata.deviceId) query.set('device_id', metadata.deviceId)
  if (metadata.logicalClientId) query.set('logical_client_id', metadata.logicalClientId)
  if (metadata.physicalHostId) query.set('physical_host_id', metadata.physicalHostId)
  if (metadata.networkProfileId) {
    query.set('network_profile_id', metadata.networkProfileId)
  }

  const uri = event.uri || req.path
  const queryString = query.toString()
  const document = {
    '@timestamp': now.toISOString(),
    timestamp: now.toISOString(),
    event_source: 'ott-api',
    event_kind: event.kind,
    request_method: req.method,
    uri,
    request_uri: queryString ? `${uri}?${queryString}` : uri,
    query_string: queryString,
    status: Number(event.status || 200),
    bytes_sent: 0,
    response_time_ms: 0,
    client_ip: requestClientIp(req),
    edge_server: safeTag(req.get('x-edge-id') || event.edgeId || 'edge-local', 32),
    token_user_id: event.userId ? String(event.userId) : '-',
    token_session_id: event.sessionId ? String(event.sessionId) : '-',
    token_content_id: event.contentId ? String(event.contentId) : '-',
    cdn_token_id: tokenGraphId(event.token),
    token_issued_at: issuedAt || '-',
    token_expires: expiresAt || '-',
    token_ttl_sec: tokenTtlSec,
    token_ttl_remaining_sec: tokenTtlSec,
    token_valid: Boolean(event.token),
    token_edge_match: Boolean(event.token),
    token_label: tokenPayload.lbl || metadata.label || '-',
    token_run_id: tokenPayload.rid || metadata.runId || '-',
    token_scenario_id: tokenPayload.scn || metadata.scenarioId || '-',
    token_dataset_label: tokenPayload.dsl || metadata.datasetLabel || '-',
    token_device_id: tokenPayload.dev || metadata.deviceId || '-',
    token_logical_client_id: tokenPayload.lc || metadata.logicalClientId || '-',
    token_physical_host_id: tokenPayload.ph || metadata.physicalHostId || '-',
    token_network_profile_id: tokenPayload.np || metadata.networkProfileId || '-',
    http_referer: req.get('referer') || '-',
    http_user_agent: req.get('user-agent') || '-',
    cache_status: '-',
    request_id: `api-${crypto.randomUUID()}`,
  }

  const dateSuffix = now.toISOString().slice(0, 10).replaceAll('-', '.')
  try {
    await esClient.index({
      index: `ott-api-events-${dateSuffix}`,
      document,
    })
    return true
  } catch (error) {
    console.error(`API telemetry index failed for ${event.kind}:`, error.message)
    return false
  }
}

module.exports = {
  emitApiEvent,
  metadataFromRequest,
  requestClientIp,
  tokenGraphId,
}

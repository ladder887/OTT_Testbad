const crypto = require('crypto')
const { tokenGraphIdFromJti } = require('./cdnToken')

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

function tokenGraphId(token, tokenPayload = {}) {
  if (tokenPayload.jti) {
    return tokenGraphIdFromJti(tokenPayload.jti)
  }
  if (!token) return ''
  const digest = crypto.createHash('sha256').update(String(token)).digest('hex')
  return `cdn_${digest.slice(0, 24)}`
}

async function emitApiEvent(req, event) {
  const esClient = req.app.locals.esClient
  if (!esClient) return false

  const now = new Date()
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

  const uri = event.uri || req.path
  const cdnTokenId = tokenGraphId(event.token, tokenPayload)
  const document = {
    '@timestamp': now.toISOString(),
    timestamp: now.toISOString(),
    event_time_epoch: now.getTime() / 1000,
    event_source: 'ott-api',
    event_kind: event.kind,
    request_method: req.method,
    uri,
    request_uri: uri,
    query_string: '-',
    status: Number(event.status || 200),
    bytes_sent: 0,
    response_time_ms: 0,
    client_ip: requestClientIp(req),
    edge_server: safeTag(req.get('x-edge-id') || event.edgeId || 'edge-local', 32),
    token_jti: tokenPayload.jti || '-',
    cdn_token_id: cdnTokenId,
    token_owner_account_id: event.userId ? String(event.userId) : '-',
    token_owner_auth_session_id: event.sessionId ? String(event.sessionId) : '-',
    token_playback_id: event.playbackId || tokenPayload.pid || '-',
    token_owner_device_id: event.ownerDeviceId || tokenPayload.odv || '-',
    token_content_id: event.contentId ? String(event.contentId) : '-',
    token_issued_at: issuedAt || '-',
    token_expires: expiresAt || '-',
    token_ttl_sec: tokenTtlSec,
    token_ttl_remaining_sec: tokenTtlSec,
    token_valid: Boolean(event.token),
    token_edge_match: Boolean(event.token),
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
  requestClientIp,
  tokenGraphId,
}

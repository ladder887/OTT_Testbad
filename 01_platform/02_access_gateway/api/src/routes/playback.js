const express = require('express')
const crypto = require('crypto')
const { authenticateToken } = require('./auth')
const { getContentById } = require('../services/contentService')
const { normalizeClientIp, getEdgeById, getEdgeForIp } = require('../services/edgeSelector')
const { issuePlaybackToken, verifyPlaybackToken } = require('../services/cdnToken')
const { emitApiEvent } = require('../services/telemetry')

const router = express.Router()

function sanitizeCollectionTag(value, { maxLength = 48, toUpper = false, toLower = false } = {}) {
  if (value === undefined || value === null) {
    return ''
  }

  let normalized = String(value)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '')
    .slice(0, maxLength)

  if (toUpper) {
    normalized = normalized.toUpperCase()
  }
  if (toLower) {
    normalized = normalized.toLowerCase()
  }

  return normalized
}

function sanitizeLabel(value) {
  if (value === undefined || value === null) {
    return ''
  }

  return String(value)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '')
    .slice(0, 48)
}

function readMetadataValue(req, name, aliases = [], options = {}) {
  const candidates = [name, ...aliases]
  for (const key of candidates) {
    const bodyValue = req.body?.[key]
    if (bodyValue !== undefined && bodyValue !== null && String(bodyValue).trim() !== '') {
      return sanitizeCollectionTag(bodyValue, options)
    }
    const queryValue = req.query?.[key]
    if (queryValue !== undefined && queryValue !== null && String(queryValue).trim() !== '') {
      return sanitizeCollectionTag(queryValue, options)
    }
  }

  return ''
}

function getRequestClientIp(req) {
  const headerIp = req.headers['x-real-ip'] || req.headers['x-forwarded-for']
  const clientIp = headerIp || req.ip || req.socket?.remoteAddress || ''
  return normalizeClientIp(clientIp)
}

async function getUserProfile(pgPool, userId) {
  try {
    const result = await pgPool.query(
      'SELECT id, email, username, subscription_plan FROM users WHERE id = $1',
      [userId]
    )
    if (result.rows.length === 0) return null
    return result.rows[0]
  } catch (error) {
    return null
  }
}

async function resolveSessionToken(pgPool, userId, fallbackToken) {
  if (fallbackToken) {
    return fallbackToken
  }

  try {
    const result = await pgPool.query(
      `SELECT session_token
       FROM sessions
       WHERE user_id = $1 AND is_active = true AND expires_at > NOW()
       ORDER BY last_activity DESC
       LIMIT 1`,
      [userId]
    )
    return result.rows[0]?.session_token || `sess_${crypto.randomUUID()}`
  } catch (error) {
    return `sess_${crypto.randomUUID()}`
  }
}

router.post('/start', authenticateToken, async (req, res) => {
  const contentId = req.body?.content_id || req.query?.content_id

  if (!contentId) {
    return res.status(400).json({ error: 'content_id is required' })
  }

  const pgPool = req.app.locals.pgPool
  const content = await getContentById(pgPool, contentId)
  if (!content) {
    return res.status(404).json({ error: '콘텐츠를 찾을 수 없습니다.' })
  }

  const clientIp = getRequestClientIp(req)
  // Edge gateways overwrite X-Edge-ID before proxying to this API. IP-based
  // selection remains a fallback for local and legacy direct API calls.
  const edge = getEdgeById(req.get('x-edge-id')) || getEdgeForIp(clientIp)
  const label = sanitizeLabel(req.body?.label || req.query?.label || req.body?.scenario_label || req.query?.scenario_label || req.body?.dataset_label || req.query?.dataset_label)
  const runId = sanitizeCollectionTag(req.body?.run_id || req.query?.run_id, { maxLength: 48 })
  const scenarioId = sanitizeCollectionTag(req.body?.scenario_id || req.query?.scenario_id, { maxLength: 24, toUpper: true })
  const datasetLabel = sanitizeCollectionTag(req.body?.dataset_label || req.query?.dataset_label, { maxLength: 24, toLower: true })
  const deviceId = readMetadataValue(req, 'device_id', ['deviceId'], { maxLength: 64 })
  const logicalClientId = readMetadataValue(req, 'logical_client_id', ['logicalClientId', 'client_id'], { maxLength: 64 })
  const physicalHostId = readMetadataValue(req, 'physical_host_id', ['physicalHostId'], { maxLength: 64 })
  const networkProfileId = readMetadataValue(req, 'network_profile_id', ['networkProfileId'], { maxLength: 32, toUpper: true })

  try {
    const user = await getUserProfile(pgPool, req.user.userId)
    if (!user) {
      return res.status(404).json({ error: '사용자를 찾을 수 없습니다.' })
    }

    const sessionToken = await resolveSessionToken(
      pgPool,
      req.user.userId,
      req.cookies?.sessionToken || req.body?.session_token || req.query?.session_token || req.query?.session_id || req.query?.sid
    )

    const issued = issuePlaybackToken({
      userId: user.id,
      sessionId: sessionToken,
      contentId: content.id,
      hlsPath: content.hlsPath,
      edgeId: edge.id,
      clientIp,
      ipBind: false,
      label,
      runId,
      scenarioId,
      datasetLabel,
      deviceId,
      logicalClientId,
      physicalHostId,
      networkProfileId,
    })

    const manifestBase = `${edge.url}/hls/${content.hlsPath}/master.m3u8`
    const manifestUrlObject = new URL(manifestBase)
    manifestUrlObject.searchParams.set('token', issued.token)
    manifestUrlObject.searchParams.set('sig', issued.sig)
    if (runId) manifestUrlObject.searchParams.set('run_id', runId)
    if (scenarioId) manifestUrlObject.searchParams.set('scenario_id', scenarioId)
    if (label) manifestUrlObject.searchParams.set('label', label)
    if (datasetLabel) manifestUrlObject.searchParams.set('dataset_label', datasetLabel)
    if (deviceId) manifestUrlObject.searchParams.set('device_id', deviceId)
    if (logicalClientId) manifestUrlObject.searchParams.set('logical_client_id', logicalClientId)
    if (physicalHostId) manifestUrlObject.searchParams.set('physical_host_id', physicalHostId)
    if (networkProfileId) manifestUrlObject.searchParams.set('network_profile_id', networkProfileId)
    const manifestUrl = manifestUrlObject.toString()

    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'playback_start', $2, $3, $4)`,
      [
        user.id,
        clientIp,
        req.get('user-agent') || '',
        JSON.stringify({
          content_id: content.id,
          hls_path: content.hlsPath,
          edge_id: edge.id,
          edge_region: edge.region,
          session_token: sessionToken,
          label: label || null,
          run_id: runId || null,
          scenario_id: scenarioId || null,
          dataset_label: datasetLabel || null,
          device_id: deviceId || null,
          logical_client_id: logicalClientId || null,
          physical_host_id: physicalHostId || null,
          network_profile_id: networkProfileId || null,
        }),
      ]
    )

    await emitApiEvent(req, {
      kind: 'token_issued',
      uri: '/api/playback/start',
      status: 200,
      userId: user.id,
      sessionId: sessionToken,
      contentId: content.id,
      edgeId: edge.id,
      token: issued.token,
      tokenPayload: issued.payload,
    })

    res.json({
      session_id: sessionToken,
      edge: edge.id,
      edge_region: edge.region,
      manifest_url: manifestUrl,
      token_expires: new Date(issued.payload.exp * 1000).toISOString(),
      stream_params: {
        token: issued.token,
        sig: issued.sig,
        content_id: content.id,
        session_id: sessionToken,
        user_id: String(user.id),
        username: user.username,
        label: issued.payload.lbl || '',
        run_id: issued.payload.rid || '',
        scenario_id: issued.payload.scn || '',
        dataset_label: issued.payload.dsl || issued.payload.lbl || '',
        device_id: issued.payload.dev || '',
        logical_client_id: issued.payload.lc || '',
        physical_host_id: issued.payload.ph || '',
        network_profile_id: issued.payload.np || '',
      },
    })
  } catch (error) {
    console.error('Playback start error:', error)
    res.status(500).json({ error: '재생 시작 처리 중 오류가 발생했습니다.' })
  }
})

router.post('/verify', async (req, res) => {
  const { token, sig, edge_id: edgeId, request_uri: requestUri, client_ip: clientIp } = req.body || {}

  if (!token || !sig || !edgeId || !requestUri) {
    return res.status(400).json({ valid: false, reason: 'missing_required_fields' })
  }

  const verified = verifyPlaybackToken({
    token,
    sig,
    edgeId,
    requestUri,
    clientIp: normalizeClientIp(clientIp),
  })

  if (!verified.valid) {
    return res.status(403).json({
      valid: false,
      reason: verified.reason,
      token_edge_match: verified.reason !== 'edge_mismatch',
    })
  }

  const payload = verified.payload
  const issuedAt = Number(payload.iat || 0)
  const expiresAt = Number(payload.exp || 0)
  const now = Math.floor(Date.now() / 1000)
  return res.json({
    valid: true,
    token_user_id: payload.uid,
    token_session_id: payload.sid,
    token_content_id: payload.cid,
    token_issued_at: issuedAt > 0 ? new Date(issuedAt * 1000).toISOString() : '-',
    token_expires: new Date(Number(payload.exp) * 1000).toISOString(),
    token_ttl_sec: issuedAt > 0 && expiresAt > issuedAt ? expiresAt - issuedAt : 0,
    token_ttl_remaining_sec: expiresAt > now ? expiresAt - now : 0,
    token_edge_match: payload.edge === edgeId,
    token_label: payload.lbl || '-',
    token_run_id: payload.rid || '-',
    token_scenario_id: payload.scn || '-',
    token_dataset_label: payload.dsl || payload.lbl || '-',
    token_device_id: payload.dev || '-',
    token_logical_client_id: payload.lc || '-',
    token_physical_host_id: payload.ph || '-',
    token_network_profile_id: payload.np || '-',
  })
})

module.exports = router

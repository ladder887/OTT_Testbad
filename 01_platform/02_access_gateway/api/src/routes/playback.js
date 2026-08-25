const express = require('express')
const crypto = require('crypto')
const { authenticateToken } = require('./auth')
const { getContentById } = require('../services/contentService')
const { normalizeClientIp, getEdgeById, getEdgeForIp } = require('../services/edgeSelector')
const {
  issuePlaybackToken,
  verifyPlaybackToken,
  tokenGraphIdFromJti,
} = require('../services/cdnToken')
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

function readBodyValue(req, name, aliases = [], options = {}) {
  const candidates = [name, ...aliases]
  for (const key of candidates) {
    const bodyValue = req.body?.[key]
    if (bodyValue !== undefined && bodyValue !== null && String(bodyValue).trim() !== '') {
      return sanitizeCollectionTag(bodyValue, options)
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
  const ownerDeviceId = readBodyValue(req, 'device_id', ['deviceId'], { maxLength: 64 })

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
    const playbackId = crypto.randomUUID()

    const issued = issuePlaybackToken({
      userId: user.id,
      sessionId: sessionToken,
      playbackId,
      contentId: content.id,
      hlsPath: content.hlsPath,
      edgeId: edge.id,
      clientIp,
      ipBind: false,
      ownerDeviceId,
    })
    const cdnTokenId = tokenGraphIdFromJti(issued.payload.jti)

    const manifestBase = `${edge.url}/hls/${content.hlsPath}/master.m3u8`
    const manifestUrlObject = new URL(manifestBase)
    manifestUrlObject.searchParams.set('token', issued.token)
    manifestUrlObject.searchParams.set('sig', issued.sig)
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
          playback_id: playbackId,
          token_jti: issued.payload.jti,
          cdn_token_id: cdnTokenId,
          owner_device_id: ownerDeviceId || null,
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
      playbackId,
      ownerDeviceId,
    })

    res.json({
      session_id: sessionToken,
      playback_id: playbackId,
      edge: edge.id,
      edge_region: edge.region,
      manifest_url: manifestUrl,
      token_expires: new Date(issued.payload.exp * 1000).toISOString(),
      token_binding: {
        token_jti: issued.payload.jti,
        cdn_token_id: cdnTokenId,
        playback_id: playbackId,
        content_id: String(content.id),
        issued_at: new Date(issued.payload.iat * 1000).toISOString(),
        owner_device_id: ownerDeviceId || null,
      },
      stream_params: {
        token: issued.token,
        sig: issued.sig,
        token_jti: issued.payload.jti,
        cdn_token_id: cdnTokenId,
        content_id: content.id,
        playback_id: playbackId,
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
    token_jti: payload.jti,
    cdn_token_id: tokenGraphIdFromJti(payload.jti),
    token_owner_account_id: payload.uid,
    token_owner_auth_session_id: payload.sid,
    token_playback_id: payload.pid,
    token_owner_device_id: payload.odv || '-',
    token_content_id: payload.cid,
    token_issued_at: issuedAt > 0 ? new Date(issuedAt * 1000).toISOString() : '-',
    token_expires: new Date(Number(payload.exp) * 1000).toISOString(),
    token_ttl_sec: issuedAt > 0 && expiresAt > issuedAt ? expiresAt - issuedAt : 0,
    token_ttl_remaining_sec: expiresAt > now ? expiresAt - now : 0,
    token_edge_match: payload.edge === edgeId,
  })
})

module.exports = router

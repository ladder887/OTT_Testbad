const express = require('express')
const crypto = require('crypto')
const { authenticateToken } = require('./auth')
const { getContentById } = require('../services/contentService')
const { normalizeClientIp, getEdgeForIp } = require('../services/edgeSelector')
const { issuePlaybackToken, verifyPlaybackToken } = require('../services/cdnToken')

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
  const { content_id: contentId } = req.body || {}

  if (!contentId) {
    return res.status(400).json({ error: 'content_id is required' })
  }

  const pgPool = req.app.locals.pgPool
  const content = await getContentById(pgPool, contentId)
  if (!content) {
    return res.status(404).json({ error: '콘텐츠를 찾을 수 없습니다.' })
  }

  const clientIp = getRequestClientIp(req)
  const edge = getEdgeForIp(clientIp)
  const label = sanitizeLabel(req.body?.label || req.body?.scenario_label || req.body?.dataset_label)
  const runId = sanitizeCollectionTag(req.body?.run_id, { maxLength: 48 })
  const scenarioId = sanitizeCollectionTag(req.body?.scenario_id, { maxLength: 24, toUpper: true })
  const datasetLabel = sanitizeCollectionTag(req.body?.dataset_label, { maxLength: 24, toLower: true })

  try {
    const user = await getUserProfile(pgPool, req.user.userId)
    if (!user) {
      return res.status(404).json({ error: '사용자를 찾을 수 없습니다.' })
    }

    const sessionToken = await resolveSessionToken(
      pgPool,
      req.user.userId,
      req.cookies?.sessionToken || req.body?.session_token
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
    })

    const manifestBase = `${edge.url}/hls/${content.hlsPath}/master.m3u8`
    const manifestUrlObject = new URL(manifestBase)
    manifestUrlObject.searchParams.set('token', issued.token)
    manifestUrlObject.searchParams.set('sig', issued.sig)
    if (runId) manifestUrlObject.searchParams.set('run_id', runId)
    if (scenarioId) manifestUrlObject.searchParams.set('scenario_id', scenarioId)
    if (label) manifestUrlObject.searchParams.set('label', label)
    if (datasetLabel) manifestUrlObject.searchParams.set('dataset_label', datasetLabel)
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
        }),
      ]
    )

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
        dataset_label: issued.payload.lbl || '',
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
  return res.json({
    valid: true,
    token_user_id: payload.uid,
    token_session_id: payload.sid,
    token_content_id: payload.cid,
    token_expires: new Date(Number(payload.exp) * 1000).toISOString(),
    token_edge_match: payload.edge === edgeId,
    token_label: payload.lbl || '-',
    token_run_id: payload.rid || '-',
    token_scenario_id: payload.scn || '-',
    token_dataset_label: payload.lbl || '-',
  })
})

module.exports = router

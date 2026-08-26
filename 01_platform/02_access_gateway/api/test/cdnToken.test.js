const assert = require('node:assert/strict')
const test = require('node:test')

process.env.CDN_SHARED_SECRET = 'test-shared-secret'
process.env.CDN_TOKEN_TTL_SEC = '1800'

const {
  issuePlaybackToken,
  verifyPlaybackToken,
  tokenGraphIdFromJti,
} = require('../src/services/cdnToken')
const { emitApiEvent } = require('../src/services/telemetry')

function decodePayload(token) {
  return JSON.parse(Buffer.from(token, 'base64url').toString('utf8'))
}

test('playback token contains operational claims only', () => {
  const issued = issuePlaybackToken({
    userId: 7,
    sessionId: 'auth-session-1',
    playbackId: 'playback-1',
    contentId: 'video_01',
    hlsPath: 'video_01',
    edgeId: 'edge-kr',
    clientIp: '192.168.0.151',
    ownerDeviceId: 'device-001',
    runId: 'run-should-not-be-signed',
    scenarioId: 'A1',
    label: 'attack',
  })
  const payload = decodePayload(issued.token)

  assert.equal(payload.pid, 'playback-1')
  assert.equal(payload.odv, 'device-001')
  assert.match(payload.jti, /^[0-9a-f-]{36}$/)
  for (const forbidden of ['rid', 'scn', 'lbl', 'dsl', 'lc', 'ph', 'np']) {
    assert.equal(Object.hasOwn(payload, forbidden), false)
  }

  const verified = verifyPlaybackToken({
    token: issued.token,
    sig: issued.sig,
    edgeId: 'edge-kr',
    requestUri: '/hls/video_01/720p/seg_00001.ts?token=hidden',
    clientIp: '192.168.0.151',
  })
  assert.equal(verified.valid, true)
})

test('jti is signed and maps to a stable graph identifier', () => {
  const issued = issuePlaybackToken({
    userId: 7,
    sessionId: 'auth-session-1',
    playbackId: 'playback-1',
    contentId: 'video_01',
    hlsPath: 'video_01',
    edgeId: 'edge-kr',
    clientIp: '192.168.0.151',
  })
  const payload = decodePayload(issued.token)
  const tampered = {
    ...payload,
    jti: '00000000-0000-4000-8000-000000000000',
  }
  const tamperedToken = Buffer.from(JSON.stringify(tampered), 'utf8').toString('base64url')

  assert.equal(
    verifyPlaybackToken({
      token: tamperedToken,
      sig: issued.sig,
      edgeId: 'edge-kr',
      requestUri: '/hls/video_01/master.m3u8',
      clientIp: '192.168.0.151',
    }).reason,
    'invalid_signature'
  )
  assert.equal(tokenGraphIdFromJti(payload.jti), tokenGraphIdFromJti(payload.jti))
  assert.match(tokenGraphIdFromJti(payload.jti), /^cdn_[a-f0-9]{24}$/)
})

test('API telemetry omits experiment provenance and raw token query data', async () => {
  const issued = issuePlaybackToken({
    userId: 7,
    sessionId: 'auth-session-1',
    playbackId: 'playback-1',
    contentId: 'video_01',
    hlsPath: 'video_01',
    edgeId: 'edge-kr',
    clientIp: '192.168.0.151',
  })
  let indexedDocument
  let indexedPipeline
  const request = {
    app: {
      locals: {
        esClient: {
          index: async ({ document, pipeline }) => {
            indexedDocument = document
            indexedPipeline = pipeline
          },
        },
      },
    },
    body: { run_id: 'run-001', scenario_id: 'A1', label: 'attack' },
    query: {},
    method: 'POST',
    path: '/api/playback/start',
    ip: '192.168.0.151',
    socket: {},
    get: (name) => {
      const headers = {
        'x-real-ip': '192.168.0.151',
        'x-edge-id': 'edge-kr',
        'x-device-id': 'device_1111111111111111',
        'user-agent': 'OTT-Test/1.0',
      }
      return headers[name.toLowerCase()]
    },
  }

  await emitApiEvent(request, {
    kind: 'token_issued',
    uri: '/api/playback/start',
    userId: 7,
    sessionId: 'auth-session-1',
    playbackId: 'playback-1',
    contentId: 'video_01',
    edgeId: 'edge-kr',
    token: issued.token,
    tokenPayload: issued.payload,
  })

  assert.equal(indexedDocument.request_uri, '/api/playback/start')
  assert.equal(indexedDocument.query_string, '-')
  assert.equal(indexedDocument.token_jti, issued.payload.jti)
  assert.equal(indexedDocument.token_playback_id, 'playback-1')
  assert.equal(indexedDocument.observed_device_id, 'device_1111111111111111')
  assert.equal(indexedPipeline, 'ott-event-ingest-timestamp')
  for (const forbidden of [
    'token_label',
    'token_run_id',
    'token_scenario_id',
    'token_dataset_label',
    'token_logical_client_id',
    'token_physical_host_id',
    'token_network_profile_id',
  ]) {
    assert.equal(Object.hasOwn(indexedDocument, forbidden), false)
  }
})

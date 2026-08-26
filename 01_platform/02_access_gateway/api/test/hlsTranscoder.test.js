const test = require('node:test')
const assert = require('node:assert/strict')

const { buildLiveFfmpegArgs } = require('../src/services/hlsTranscoder')


test('live FFmpeg args cap x264 and filter threads while preserving rolling HLS', () => {
  const args = buildLiveFfmpegArgs({
    sourceFilePath: '/source/live.mp4',
    profile: {
      width: 1920,
      height: 1080,
      videoBitrate: '3000k',
      maxrate: '3300k',
      bufsize: '6000k',
      audioBitrate: '128k',
    },
    variantPlaylist: '/hls/live_01/1080p/playlist.m3u8',
    segmentPattern: '/hls/live_01/1080p/seg_%05d.ts',
  })

  assert.equal(args[args.indexOf('-preset') + 1], 'ultrafast')
  assert.equal(args[args.indexOf('-threads') + 1], '1')
  assert.equal(args[args.indexOf('-filter_threads') + 1], '1')
  assert.equal(args[args.indexOf('-stream_loop') + 1], '-1')
  assert.match(args[args.indexOf('-hls_flags') + 1], /omit_endlist/)
  assert.match(args[args.indexOf('-hls_flags') + 1], /delete_segments/)
})

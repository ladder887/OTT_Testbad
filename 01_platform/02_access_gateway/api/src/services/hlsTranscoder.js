const fs = require('fs')
const path = require('path')
const { spawn } = require('child_process')

const HLS_OUTPUT_DIR = process.env.HLS_OUTPUT_DIR || '/var/www/hls'
const FFMPEG_BIN = process.env.FFMPEG_BIN || 'ffmpeg'
const FFPROBE_BIN = process.env.FFPROBE_BIN || 'ffprobe'
const LIVE_PID_DIR = process.env.LIVE_PID_DIR || '/tmp/ott-live-transcoder'
const LIVE_HLS_TIME_SEC = String(Math.max(1, Number.parseInt(process.env.LIVE_HLS_TIME_SEC || '2', 10) || 2))
const LIVE_HLS_LIST_SIZE = String(Math.max(3, Number.parseInt(process.env.LIVE_HLS_LIST_SIZE || '12', 10) || 12))
const LIVE_X264_PRESET = process.env.LIVE_X264_PRESET || 'ultrafast'
const LIVE_X264_THREADS = String(
  Math.max(1, Number.parseInt(process.env.LIVE_X264_THREADS || '1', 10) || 1)
)

const PROFILE_ORDER = ['1080p', '720p']

const HLS_PROFILES = {
  '1080p': {
    width: 1920,
    height: 1080,
    videoBitrate: '3000k',
    maxrate: '3300k',
    bufsize: '6000k',
    audioBitrate: '128k',
    bandwidth: 3128000,
  },
  '720p': {
    width: 1280,
    height: 720,
    videoBitrate: '1500k',
    maxrate: '1650k',
    bufsize: '3000k',
    audioBitrate: '96k',
    bandwidth: 1596000,
  },
}

function normalizeResolutions(input) {
  const requested = Array.isArray(input)
    ? input
    : String(input || '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)

  const selected = PROFILE_ORDER.filter((resolution) => requested.includes(resolution))
  return selected.length > 0 ? selected : PROFILE_ORDER.slice()
}

function sanitizeHlsPath(rawPath) {
  const normalized = String(rawPath || '')
    .replace(/\\+/g, '/')
    .split('/')
    .map((segment) => segment.trim())
    .filter(Boolean)
    .map((segment) => segment.replace(/[^a-zA-Z0-9_-]/g, '_'))
    .filter(Boolean)

  if (normalized.length === 0) {
    throw new Error('유효한 hls_path가 필요합니다.')
  }

  return normalized.join('/')
}

function buildMasterPlaylist(resolutions) {
  const lines = ['#EXTM3U', '#EXT-X-VERSION:3']

  for (const resolution of resolutions) {
    const profile = HLS_PROFILES[resolution]
    lines.push(
      `#EXT-X-STREAM-INF:BANDWIDTH=${profile.bandwidth},RESOLUTION=${profile.width}x${profile.height},NAME=\"${resolution}\"`
    )
    lines.push(`${resolution}/playlist.m3u8`)
  }

  return `${lines.join('\n')}\n`
}

function getOutputRoot(safeHlsPath) {
  return path.join(HLS_OUTPUT_DIR, ...safeHlsPath.split('/'))
}

function getScaleFilter(profile) {
  return `scale=w=${profile.width}:h=${profile.height}:force_original_aspect_ratio=decrease,pad=${profile.width}:${profile.height}:(ow-iw)/2:(oh-ih)/2`
}

function buildLiveFfmpegArgs({ sourceFilePath, profile, variantPlaylist, segmentPattern }) {
  return [
    '-y',
    '-re',
    '-stream_loop',
    '-1',
    '-i',
    sourceFilePath,
    '-filter_threads',
    '1',
    '-vf',
    getScaleFilter(profile),
    '-c:v',
    'libx264',
    '-preset',
    LIVE_X264_PRESET,
    '-tune',
    'zerolatency',
    '-threads',
    LIVE_X264_THREADS,
    '-profile:v',
    'main',
    '-sc_threshold',
    '0',
    '-g',
    '48',
    '-keyint_min',
    '48',
    '-c:a',
    'aac',
    '-ar',
    '48000',
    '-b:v',
    profile.videoBitrate,
    '-maxrate',
    profile.maxrate,
    '-bufsize',
    profile.bufsize,
    '-b:a',
    profile.audioBitrate,
    '-f',
    'hls',
    '-hls_time',
    LIVE_HLS_TIME_SEC,
    '-hls_list_size',
    LIVE_HLS_LIST_SIZE,
    '-hls_flags',
    'delete_segments+append_list+independent_segments+omit_endlist',
    '-hls_segment_filename',
    segmentPattern,
    variantPlaylist,
  ]
}

function runFfmpeg(args) {
  return new Promise((resolve, reject) => {
    const ffmpeg = spawn(FFMPEG_BIN, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    let stderrTail = ''

    ffmpeg.stderr.on('data', (chunk) => {
      stderrTail = `${stderrTail}${chunk.toString()}`
      if (stderrTail.length > 10000) {
        stderrTail = stderrTail.slice(-10000)
      }
    })

    ffmpeg.on('error', (error) => {
      reject(new Error(`FFmpeg 실행 실패: ${error.message}`))
    })

    ffmpeg.on('close', (code) => {
      if (code === 0) {
        resolve()
        return
      }

      reject(new Error(`FFmpeg 변환 실패 (exit ${code}): ${stderrTail}`))
    })
  })
}

function runFfmpegDetached(args) {
  return new Promise((resolve, reject) => {
    const ffmpeg = spawn(FFMPEG_BIN, args, {
      detached: true,
      stdio: 'ignore',
    })

    ffmpeg.once('error', (error) => {
      reject(new Error(`FFmpeg 실행 실패: ${error.message}`))
    })

    ffmpeg.once('spawn', () => {
      if (!ffmpeg.pid) {
        reject(new Error('FFmpeg 백그라운드 실행 PID를 확인할 수 없습니다.'))
        return
      }

      ffmpeg.unref()
      resolve(ffmpeg.pid)
    })
  })
}

function runFfprobe(args) {
  return new Promise((resolve, reject) => {
    const ffprobe = spawn(FFPROBE_BIN, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    let stdout = ''
    let stderr = ''

    ffprobe.stdout.on('data', (chunk) => {
      stdout += chunk.toString()
    })

    ffprobe.stderr.on('data', (chunk) => {
      stderr += chunk.toString()
    })

    ffprobe.on('error', (error) => {
      reject(new Error(`FFprobe 실행 실패: ${error.message}`))
    })

    ffprobe.on('close', (code) => {
      if (code === 0) {
        resolve(stdout.trim())
        return
      }

      reject(new Error(`FFprobe 분석 실패 (exit ${code}): ${stderr}`))
    })
  })
}

function formatDurationLabel(totalSeconds) {
  const seconds = Math.max(1, Number(totalSeconds) || 0)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainSeconds = seconds % 60

  const parts = []
  if (hours > 0) {
    parts.push(`${hours}시간`)
  }
  if (minutes > 0 || hours > 0) {
    parts.push(`${minutes}분`)
  }
  parts.push(`${remainSeconds}초`)

  return parts.join(' ')
}

function getLivePidFilePath(safeHlsPath, resolution) {
  const slug = safeHlsPath.replace(/\//g, '__')
  return path.join(LIVE_PID_DIR, `${slug}_${resolution}.pid`)
}

async function stopLiveProcessByPidFile(pidFilePath) {
  let pid = null
  try {
    const raw = await fs.promises.readFile(pidFilePath, 'utf8')
    pid = Number.parseInt(String(raw || '').trim(), 10)

    if (Number.isFinite(pid) && pid > 1) {
      try {
        process.kill(pid, 'SIGTERM')
      } catch (error) {
        // 프로세스가 이미 종료된 경우는 무시한다.
      }
    }
  } catch (error) {
    // PID 파일이 없으면 무시한다.
  }

  if (Number.isFinite(pid) && pid > 1) {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      try {
        process.kill(pid, 0)
        await new Promise((resolve) => setTimeout(resolve, 100))
      } catch (error) {
        pid = null
        break
      }
    }
    if (pid) {
      try {
        process.kill(pid, 'SIGKILL')
      } catch (error) {
        // 종료 확인 직후 process가 끝난 경우는 무시한다.
      }
    }
  }

  await fs.promises.rm(pidFilePath, { force: true })
}

async function stopLiveLoopTranscode({ hlsPath, resolutions }) {
  const safeHlsPath = sanitizeHlsPath(hlsPath)
  const selectedResolutions = normalizeResolutions(resolutions)

  await fs.promises.mkdir(LIVE_PID_DIR, { recursive: true })

  for (const resolution of selectedResolutions) {
    const pidFilePath = getLivePidFilePath(safeHlsPath, resolution)
    await stopLiveProcessByPidFile(pidFilePath)
  }
}

async function removeHlsOutput(hlsPath) {
  const safeHlsPath = sanitizeHlsPath(hlsPath)
  const outputRoot = getOutputRoot(safeHlsPath)
  await fs.promises.rm(outputRoot, { recursive: true, force: true })
  return outputRoot
}

async function probeSourceDuration(sourceFilePath) {
  if (!sourceFilePath) {
    throw new Error('길이 분석 대상 파일 경로가 필요합니다.')
  }

  const output = await runFfprobe([
    '-v',
    'error',
    '-show_entries',
    'format=duration',
    '-of',
    'default=noprint_wrappers=1:nokey=1',
    sourceFilePath,
  ])

  const durationFloat = Number.parseFloat(output)
  if (!Number.isFinite(durationFloat) || durationFloat <= 0) {
    throw new Error(`영상 길이 분석 실패: duration='${output}'`)
  }

  const durationSec = Math.max(1, Math.round(durationFloat))
  return {
    durationSec,
    durationLabel: formatDurationLabel(durationSec),
  }
}

async function transcodeSourceToHls({
  sourceFilePath,
  hlsPath,
  resolutions,
  targetDurationSec,
  sourceDurationSec,
}) {
  if (!sourceFilePath) {
    throw new Error('소스 영상 파일 경로가 필요합니다.')
  }

  const safeHlsPath = sanitizeHlsPath(hlsPath)
  const selectedResolutions = normalizeResolutions(resolutions)
  const outputRoot = getOutputRoot(safeHlsPath)
  const normalizedTargetDurationSec =
    Number.isFinite(targetDurationSec) && Number(targetDurationSec) > 0
      ? Math.max(1, Math.round(Number(targetDurationSec)))
      : null

  let normalizedSourceDurationSec =
    Number.isFinite(sourceDurationSec) && Number(sourceDurationSec) > 0
      ? Math.max(1, Math.round(Number(sourceDurationSec)))
      : null

  if (normalizedTargetDurationSec && !normalizedSourceDurationSec) {
    const sourceInfo = await probeSourceDuration(sourceFilePath)
    normalizedSourceDurationSec = sourceInfo.durationSec
  }

  const shouldLoopSource =
    Boolean(normalizedTargetDurationSec) &&
    Boolean(normalizedSourceDurationSec) &&
    normalizedTargetDurationSec > normalizedSourceDurationSec

  const streamLoopCount = shouldLoopSource
    ? Math.max(0, Math.ceil(normalizedTargetDurationSec / normalizedSourceDurationSec) - 1)
    : 0

  // 동일 경로에 live 루프 인코더가 떠있으면 파일 갱신 충돌이 날 수 있어 정리한다.
  await stopLiveLoopTranscode({ hlsPath: safeHlsPath, resolutions: PROFILE_ORDER })

  await fs.promises.rm(outputRoot, { recursive: true, force: true })
  await fs.promises.mkdir(outputRoot, { recursive: true })

  for (const resolution of selectedResolutions) {
    const profile = HLS_PROFILES[resolution]
    const variantDir = path.join(outputRoot, resolution)
    const variantPlaylist = path.join(variantDir, 'playlist.m3u8')
    const segmentPattern = path.join(variantDir, 'seg_%05d.ts')

    await fs.promises.mkdir(variantDir, { recursive: true })

    const args = ['-y']

    if (streamLoopCount > 0) {
      args.push('-stream_loop', String(streamLoopCount))
    }

    args.push('-i', sourceFilePath)

    if (normalizedTargetDurationSec) {
      args.push('-t', String(normalizedTargetDurationSec))
    }

    args.push(
      '-vf',
      getScaleFilter(profile),
      '-c:v',
      'libx264',
      '-preset',
      'veryfast',
      '-profile:v',
      'main',
      '-sc_threshold',
      '0',
      '-g',
      '48',
      '-keyint_min',
      '48',
      '-c:a',
      'aac',
      '-ar',
      '48000',
      '-b:v',
      profile.videoBitrate,
      '-maxrate',
      profile.maxrate,
      '-bufsize',
      profile.bufsize,
      '-b:a',
      profile.audioBitrate,
      '-f',
      'hls',
      '-hls_time',
      '6',
      '-hls_playlist_type',
      'vod',
      '-hls_flags',
      'independent_segments',
      '-hls_segment_filename',
      segmentPattern,
      variantPlaylist,
    )

    await runFfmpeg(args)
  }

  const masterPlaylistPath = path.join(outputRoot, 'master.m3u8')
  await fs.promises.writeFile(masterPlaylistPath, buildMasterPlaylist(selectedResolutions), 'utf8')

  return {
    hlsPath: safeHlsPath,
    resolutions: selectedResolutions,
    outputRoot,
    masterPlaylistPath,
    mode: 'vod',
    outputDurationSec: normalizedTargetDurationSec || normalizedSourceDurationSec || null,
    outputDurationLabel:
      normalizedTargetDurationSec || normalizedSourceDurationSec
        ? formatDurationLabel(normalizedTargetDurationSec || normalizedSourceDurationSec)
        : null,
  }
}

async function startLiveLoopTranscode({ sourceFilePath, hlsPath, resolutions }) {
  if (!sourceFilePath) {
    throw new Error('라이브 소스 영상 파일 경로가 필요합니다.')
  }

  const safeHlsPath = sanitizeHlsPath(hlsPath)
  const selectedResolutions = normalizeResolutions(resolutions)
  const outputRoot = getOutputRoot(safeHlsPath)

  await stopLiveLoopTranscode({ hlsPath: safeHlsPath, resolutions: PROFILE_ORDER })
  await fs.promises.rm(outputRoot, { recursive: true, force: true })
  await fs.promises.mkdir(outputRoot, { recursive: true })
  await fs.promises.mkdir(LIVE_PID_DIR, { recursive: true })

  const liveProcesses = []
  const startedPidFiles = []

  try {
    for (const resolution of selectedResolutions) {
      const profile = HLS_PROFILES[resolution]
      const variantDir = path.join(outputRoot, resolution)
      const variantPlaylist = path.join(variantDir, 'playlist.m3u8')
      const segmentPattern = path.join(variantDir, 'seg_%05d.ts')
      const pidFilePath = getLivePidFilePath(safeHlsPath, resolution)

      await fs.promises.mkdir(variantDir, { recursive: true })
      await stopLiveProcessByPidFile(pidFilePath)

      const args = buildLiveFfmpegArgs({
        sourceFilePath,
        profile,
        variantPlaylist,
        segmentPattern,
      })

      const pid = await runFfmpegDetached(args)
      await fs.promises.writeFile(pidFilePath, String(pid), 'utf8')

      startedPidFiles.push(pidFilePath)
      liveProcesses.push({ resolution, pid })
    }
  } catch (error) {
    for (const pidFilePath of startedPidFiles) {
      await stopLiveProcessByPidFile(pidFilePath)
    }
    throw error
  }

  const masterPlaylistPath = path.join(outputRoot, 'master.m3u8')
  await fs.promises.writeFile(masterPlaylistPath, buildMasterPlaylist(selectedResolutions), 'utf8')

  return {
    hlsPath: safeHlsPath,
    resolutions: selectedResolutions,
    outputRoot,
    masterPlaylistPath,
    mode: 'live',
    processes: liveProcesses,
  }
}

module.exports = {
  transcodeSourceToHls,
  startLiveLoopTranscode,
  stopLiveLoopTranscode,
  removeHlsOutput,
  normalizeResolutions,
  probeSourceDuration,
  buildLiveFfmpegArgs,
}

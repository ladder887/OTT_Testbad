import { useEffect, useRef, useState } from 'react'
import { useParams, useLocation, useNavigate } from 'react-router-dom'
import Hls from 'hls.js'
import { playbackAPI } from '../services/api'
import './WatchPage.css'

function WatchPage() {
  const { contentId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const videoRef = useRef(null)
  const hlsRef = useRef(null)
  const mountedRef = useRef(false)

  const [content, setContent] = useState(location.state?.content || null)
  const [error, setError] = useState(null)
  const [availableLevels, setAvailableLevels] = useState([])
  const [selectedLevel, setSelectedLevel] = useState(-1)

  const collectionParams = (() => {
    const query = new URLSearchParams(location.search || '')
    const label = query.get('label') || query.get('dataset_label') || 'normal'
    const runId = query.get('run_id') || ''
    const scenarioId = query.get('scenario_id') || ''

    return {
      label,
      run_id: runId,
      scenario_id: scenarioId,
      dataset_label: label,
    }
  })()

  useEffect(() => {
    if (!content) {
      setError('콘텐츠 정보를 찾을 수 없습니다.')
      return
    }

    // 첫 마운트가 아니면 스킵 (React Strict Mode 대응)
    if (mountedRef.current) {
      console.log('HLS already initialized, skipping')
      return
    }

    mountedRef.current = true

    const setupVideo = async () => {
      const video = videoRef.current
      if (!video) return

      // 이전 HLS 인스턴스 정리
      if (hlsRef.current) {
        console.log('Destroying previous HLS instance')
        hlsRef.current.destroy()
        hlsRef.current = null
      }

      const userId = localStorage.getItem('userId') || ''
      const username = localStorage.getItem('username') || 'guest'

      const { data: playbackData } = await playbackAPI.start(content.id, collectionParams)
      const videoUrl = playbackData?.manifest_url
      if (!videoUrl) {
        setError('재생 URL을 가져오지 못했습니다.')
        return
      }

      const streamParams = playbackData?.stream_params || {}
      const manifestUrl = new URL(videoUrl)
      const token = streamParams.token || manifestUrl.searchParams.get('token') || ''
      const sig = streamParams.sig || manifestUrl.searchParams.get('sig') || ''
      const streamLabel = streamParams.label || collectionParams.label || 'normal'
      const streamContentId = streamParams.content_id || content.id
      const streamSessionId = streamParams.session_id || playbackData?.session_id || ''
      const streamUserId = streamParams.user_id || userId
      const streamUsername = streamParams.username || username
      const streamRunId = streamParams.run_id || collectionParams.run_id || ''
      const streamScenarioId = streamParams.scenario_id || collectionParams.scenario_id || ''
      const streamDatasetLabel = streamParams.dataset_label || collectionParams.dataset_label || streamLabel

      console.log('Start Video Playback:', {
        contentId: content.id,
        userId: streamUserId,
        username: streamUsername,
        token: token ? `${token.substring(0, 20)}...` : 'n/a',
        signature: sig ? `${sig.substring(0, 20)}...` : 'n/a',
        sessionId: streamSessionId,
        url: videoUrl
      })

      // HLS.js 설정
      console.log('Initializing HLS for:', videoUrl)
      
      if (Hls.isSupported()) {
        class SignedQueryLoader extends Hls.DefaultConfig.loader {
          load(context, config, callbacks) {
            if (context?.url && (context.url.includes('.m3u8') || context.url.includes('.ts'))) {
              const resolvedUrl = new URL(context.url, window.location.origin)
              if (token && !resolvedUrl.searchParams.get('token')) {
                resolvedUrl.searchParams.set('token', token)
              }
              if (sig && !resolvedUrl.searchParams.get('sig')) {
                resolvedUrl.searchParams.set('sig', sig)
              }
              if (streamContentId && !resolvedUrl.searchParams.get('content_id')) {
                resolvedUrl.searchParams.set('content_id', streamContentId)
              }
              if (streamUserId && !resolvedUrl.searchParams.get('user_id')) {
                resolvedUrl.searchParams.set('user_id', streamUserId)
              }
              if (streamUsername && !resolvedUrl.searchParams.get('user')) {
                resolvedUrl.searchParams.set('user', streamUsername)
              }
              if (streamSessionId && !resolvedUrl.searchParams.get('sid')) {
                resolvedUrl.searchParams.set('sid', streamSessionId)
              }
              if (streamLabel && !resolvedUrl.searchParams.get('label')) {
                resolvedUrl.searchParams.set('label', streamLabel)
              }
              if (streamRunId && !resolvedUrl.searchParams.get('run_id')) {
                resolvedUrl.searchParams.set('run_id', streamRunId)
              }
              if (streamScenarioId && !resolvedUrl.searchParams.get('scenario_id')) {
                resolvedUrl.searchParams.set('scenario_id', streamScenarioId)
              }
              if (streamDatasetLabel && !resolvedUrl.searchParams.get('dataset_label')) {
                resolvedUrl.searchParams.set('dataset_label', streamDatasetLabel)
              }

              context.url = resolvedUrl.toString()
            }

            super.load(context, config, callbacks)
          }
        }

        const hls = new Hls({
          debug: true,  // 디버그 활성화
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 90,
          maxBufferLength: 30,
          maxMaxBufferLength: 600,
          loader: SignedQueryLoader,
          xhrSetup: function(xhr, url) {
            console.log('XHR Setup:', url)
            xhr.withCredentials = false
          }
        })

        console.log('Loading HLS source:', videoUrl)
        hls.loadSource(videoUrl)
        hls.attachMedia(video)

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          console.log('✓ HLS manifest parsed successfully')

          const levels = hls.levels.map((level, index) => ({
            index,
            height: level.height,
            bitrate: level.bitrate,
            label: level.height ? `${level.height}p` : `${Math.round((level.bitrate || 0) / 1000)}kbps`,
          }))
          setAvailableLevels(levels)

          if (content?.contentType === 'live') {
            const preferred = content?.availableResolutions || []
            const preferredLevel = levels.find((item) => preferred.includes(item.label))
            if (preferredLevel) {
              hls.currentLevel = preferredLevel.index
              setSelectedLevel(preferredLevel.index)
            } else {
              setSelectedLevel(-1)
            }
          }

          video.play().catch(e => console.warn('Auto-play prevented:', e))
        })

        hls.on(Hls.Events.MANIFEST_LOADING, (event, data) => {
          console.log('→ Manifest loading:', data.url)
        })

        hls.on(Hls.Events.MANIFEST_LOADED, (event, data) => {
          console.log('✓ Manifest loaded:', data)
        })

        hls.on(Hls.Events.ERROR, (event, data) => {
          console.error('HLS error:', data)
          if (data.fatal) {
            switch (data.type) {
              case Hls.ErrorTypes.NETWORK_ERROR:
                console.error('Network error, trying to recover...')
                hls.startLoad()
                break
              case Hls.ErrorTypes.MEDIA_ERROR:
                console.error('Media error, trying to recover...')
                hls.recoverMediaError()
                break
              default:
                console.error('Fatal error, destroying HLS instance')
                hls.destroy()
                setError('비디오 재생 오류가 발생했습니다.')
                break
            }
          }
        })

        hlsRef.current = hls
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari 네이티브 HLS 지원
        video.src = videoUrl
        video.addEventListener('loadedmetadata', () => {
          video.play().catch(e => console.warn('Auto-play prevented:', e))
        })
      } else {
        setError('브라우저가 HLS 재생을 지원하지 않습니다.')
      }
    }

    setupVideo()

    return () => {
      console.log('Cleanup called')
      const video = videoRef.current
      if (video) {
        video.pause()
        video.src = ''
      }

      setAvailableLevels([])
      setSelectedLevel(-1)
      
      // HLS 정리
      if (hlsRef.current) {
        console.log('Cleanup: destroying HLS instance')
        hlsRef.current.destroy()
        hlsRef.current = null
      }
      mountedRef.current = false
    }
  }, [content])

  const handleBack = () => {
    navigate('/home')
  }

  const handleQualityChange = (event) => {
    const value = Number(event.target.value)
    setSelectedLevel(value)

    if (!hlsRef.current) return
    hlsRef.current.currentLevel = value
  }

  if (error) {
    return (
      <div className="watch-page error-page">
        <div className="error-container">
          <h2>경고</h2>
          <p>{error}</p>
          <button onClick={handleBack} className="back-button">
            뒤로 가기
          </button>
        </div>
      </div>
    )
  }

  if (!content) {
    return (
      <div className="watch-page error-page">
        <div className="error-container">
          <h2>콘텐츠를 찾을 수 없습니다</h2>
          <button onClick={handleBack} className="back-button">
            뒤로 가기
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="watch-page">
      <div className="video-container">
        <button className="back-button-overlay" onClick={handleBack}>
          뒤로
        </button>

        <video ref={videoRef} className="video-player" controls autoPlay />
      </div>

      <div className="content-details">
        <h1 className="content-title">{content.title}</h1>
        <p className="content-description">{content.description}</p>

        <div className="metadata">
          <span className="metadata-item">{content.year}</span>
          <span className="metadata-item">{content.rating}</span>
          <span className={`metadata-item ${content.contentType === 'live' ? 'status' : ''}`}>
            {content.contentType === 'live' ? 'LIVE' : 'VOD'}
          </span>
          <span className="metadata-item">{(content.availableResolutions || []).join(', ')}</span>
          <span className="metadata-item">{content.genre?.join(', ')}</span>
        </div>

        <div className="quality-control">
          <label htmlFor="quality-select">화질 설정</label>
          <select id="quality-select" value={selectedLevel} onChange={handleQualityChange}>
            <option value={-1}>자동</option>
            {availableLevels.map((level) => (
              <option key={`${level.index}-${level.label}`} value={level.index}>
                {level.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  )
}

export default WatchPage

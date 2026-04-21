import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { contentAPI, getImageUrl } from '../services/api'
import { useAuthStore } from '../store/authStore'
import './ContentManagePage.css'

const initialForm = {
  content_id: '',
  title: '',
  description: '',
  hls_path: '',
  content_type: 'vod',
  target_duration_min: '',
  year: new Date().getFullYear(),
  rating: '전체',
  genre: '영상',
  featured: false,
}

const getTypeLabel = (contentType) => (contentType === 'live' ? '라이브' : '콘텐츠')

const formatFileSize = (size) => {
  const value = Number(size) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function ContentManagePage() {
  const navigate = useNavigate()
  const { user } = useAuthStore()

  const [form, setForm] = useState(initialForm)
  const [selectedResolutions, setSelectedResolutions] = useState(['1080p', '720p'])
  const [thumbnailImage, setThumbnailImage] = useState(null)
  const [sourceVideo, setSourceVideo] = useState(null)

  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const isAdmin = useMemo(() => {
    return (user?.username || '').toLowerCase() === 'admin' || (user?.email || '').toLowerCase() === 'admin@ott.com'
  }, [user])

  const autoCategoryLabel = useMemo(() => getTypeLabel(form.content_type), [form.content_type])

  const loadItems = async () => {
    try {
      setLoading(true)
      const { data } = await contentAPI.getAdminList()
      setItems(data?.contents || [])
      setLoading(false)
    } catch (err) {
      console.error(err)
      setError(err?.response?.data?.error || '콘텐츠 목록을 불러오지 못했습니다.')
      setLoading(false)
    }
  }

  useEffect(() => {
    if (isAdmin) {
      loadItems()
    } else {
      setLoading(false)
    }
  }, [isAdmin])

  const handleTypeChange = (nextType) => {
    setForm((prev) => ({
      ...prev,
      content_type: nextType,
      target_duration_min: nextType === 'live' ? '' : prev.target_duration_min,
      genre: nextType === 'live' ? '라이브' : prev.genre === '라이브' ? '영상' : prev.genre,
    }))
  }

  const toggleResolution = (resolution) => {
    setSelectedResolutions((prev) => {
      if (prev.includes(resolution)) {
        const next = prev.filter((r) => r !== resolution)
        return next.length === 0 ? prev : next
      }
      return [...prev, resolution]
    })
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    setMessage('')
    setError('')

    if (!form.title.trim()) {
      setError('제목을 입력하세요.')
      return
    }

    if (selectedResolutions.length === 0) {
      setError('최소 1개 해상도를 선택하세요.')
      return
    }

    try {
      setSaving(true)
      const payload = new FormData()
      Object.entries(form).forEach(([key, value]) => {
        if (key === 'target_duration_min' && form.content_type === 'live') {
          return
        }
        payload.append(key, value)
      })
      payload.set('category', autoCategoryLabel)

      payload.set('available_resolutions', selectedResolutions.join(','))

      if (thumbnailImage) {
        payload.append('thumbnailImage', thumbnailImage)
      }

      if (sourceVideo) {
        payload.append('sourceVideo', sourceVideo)
      }

      const { data } = await contentAPI.uploadManaged(payload)
      setMessage(data?.message || '콘텐츠 등록 완료')
      setForm({ ...initialForm })
      setSelectedResolutions(['1080p', '720p'])
      setThumbnailImage(null)
      setSourceVideo(null)
      await loadItems()
    } catch (err) {
      console.error(err)
      setError(err?.response?.data?.error || '콘텐츠 등록에 실패했습니다.')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (item) => {
    const contentId = String(item?.id || '').trim()
    if (!contentId) return

    const confirmed = window.confirm(`'${item?.title || contentId}' 콘텐츠를 삭제하시겠습니까?`)
    if (!confirmed) return

    setMessage('')
    setError('')

    try {
      setDeletingId(contentId)
      const { data } = await contentAPI.deleteManaged(contentId)
      setMessage(data?.message || '콘텐츠가 삭제되었습니다.')
      await loadItems()
    } catch (err) {
      console.error(err)
      setError(err?.response?.data?.error || '콘텐츠 삭제에 실패했습니다.')
    } finally {
      setDeletingId('')
    }
  }

  if (!isAdmin) {
    return (
      <div className="manage-page">
        <div className="manage-container denied">
          <h2>접근 권한 없음</h2>
          <p>영상관리 페이지는 관리자 계정에서만 사용할 수 있습니다.</p>
          <button type="button" className="primary-button" onClick={() => navigate('/home')}>
            홈으로 이동
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="manage-page">
      <div className="manage-container">
        <header className="manage-header">
          <div>
            <p className="manage-kicker">OTTFLIX STUDIO</p>
            <h1>영상관리 스튜디오</h1>
            <p className="manage-subtitle">원본 업로드 시 VOD/LIVE 모두 1080p/720p HLS를 자동 생성합니다.</p>
          </div>
          <button type="button" className="ghost-button" onClick={() => navigate('/home')}>
            홈으로 돌아가기
          </button>
        </header>

        <section className="manage-form-section">
          <div className="section-headline">
            <h2>신규 콘텐츠 등록 / 업데이트</h2>
            <span className={`type-chip ${form.content_type}`}>{form.content_type.toUpperCase()}</span>
          </div>

          <form className="manage-form" onSubmit={handleSubmit}>
            <div className="form-grid">
              <label>
                콘텐츠 ID (선택)
                <input
                  type="text"
                  value={form.content_id}
                  onChange={(e) => setForm((prev) => ({ ...prev, content_id: e.target.value }))}
                  placeholder="예: live_003"
                />
              </label>

              <label>
                제목 *
                <input
                  type="text"
                  value={form.title}
                  onChange={(e) => setForm((prev) => ({ ...prev, title: e.target.value }))}
                  placeholder="예: 라이브 채널 3"
                  required
                />
              </label>

              <label>
                콘텐츠 타입
                <select value={form.content_type} onChange={(e) => handleTypeChange(e.target.value)}>
                  <option value="vod">콘텐츠(VOD)</option>
                  <option value="live">라이브(LIVE)</option>
                </select>
              </label>

              <label>
                자동 카테고리
                <input type="text" value={autoCategoryLabel} disabled />
              </label>

              <label>
                HLS 경로
                <input
                  type="text"
                  value={form.hls_path}
                  onChange={(e) => setForm((prev) => ({ ...prev, hls_path: e.target.value }))}
                  placeholder="예: live_001, cat1"
                />
              </label>

              <label>
                연도
                <input
                  type="number"
                  value={form.year}
                  onChange={(e) => setForm((prev) => ({ ...prev, year: e.target.value }))}
                />
              </label>

              <label>
                시청등급
                <input
                  type="text"
                  value={form.rating}
                  onChange={(e) => setForm((prev) => ({ ...prev, rating: e.target.value }))}
                  placeholder="예: 전체"
                />
              </label>

              <label>
                길이 처리
                <input
                  type="text"
                  value={
                    form.content_type === 'live'
                      ? 'LIVE 고정'
                      : form.target_duration_min
                        ? `원본 반복 확장 (${form.target_duration_min}분 목표)`
                        : sourceVideo
                          ? '업로드 파일 기반 자동 계산'
                          : '원본 영상 업로드 시 자동 계산'
                  }
                  disabled
                />
              </label>

              <label>
                목표 길이(분, 선택)
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.target_duration_min}
                  onChange={(e) => setForm((prev) => ({ ...prev, target_duration_min: e.target.value }))}
                  placeholder="예: 57"
                  disabled={form.content_type === 'live'}
                />
              </label>

              <label>
                장르 (콤마 구분)
                <input
                  type="text"
                  value={form.genre}
                  onChange={(e) => setForm((prev) => ({ ...prev, genre: e.target.value }))}
                  placeholder="예: 라이브,스포츠"
                />
              </label>

              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.featured}
                  onChange={(e) => setForm((prev) => ({ ...prev, featured: e.target.checked }))}
                />
                대표 배너(Featured)
              </label>
            </div>

            <div className="auto-info-row">
              <div className="auto-info-card">
                <strong>자동 분류</strong>
                <span>{autoCategoryLabel}</span>
              </div>
              <div className="auto-info-card">
                <strong>자동 길이 계산</strong>
                <span>
                  {form.content_type === 'live'
                    ? 'LIVE'
                    : form.target_duration_min
                      ? `${form.target_duration_min}분 목표로 반복 확장`
                      : sourceVideo
                        ? 'ffprobe 분석 후 저장'
                        : '원본 영상 업로드 필요'}
                </span>
              </div>
              <div className="auto-info-card">
                <strong>자동 HLS 생성</strong>
                <span>
                  {sourceVideo
                    ? form.content_type === 'live'
                      ? '실시간 루프 라이브 스트림 시작 (1080p + 720p)'
                      : '1080p + 720p + master.m3u8'
                    : '원본 영상 업로드 시 자동 생성'}
                </span>
              </div>
            </div>

            <label>
              설명
              <textarea
                value={form.description}
                onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
                rows={3}
                placeholder="콘텐츠 설명"
              />
            </label>

            <div className="resolution-row">
              <span>해상도 선택</span>
              <label>
                <input
                  type="checkbox"
                  checked={selectedResolutions.includes('1080p')}
                  onChange={() => toggleResolution('1080p')}
                />
                1080p
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={selectedResolutions.includes('720p')}
                  onChange={() => toggleResolution('720p')}
                />
                720p
              </label>
            </div>

            <div className="file-row">
              <label className="file-picker">
                썸네일 이미지 업로드
                <input type="file" accept="image/*" onChange={(e) => setThumbnailImage(e.target.files?.[0] || null)} />
                {thumbnailImage && (
                  <small className="file-meta">{thumbnailImage.name} ({formatFileSize(thumbnailImage.size)})</small>
                )}
              </label>

              <label className="file-picker">
                원본 영상 업로드 (VOD/LIVE 자동 HLS 1080p/720p 생성)
                <input type="file" accept="video/*" onChange={(e) => setSourceVideo(e.target.files?.[0] || null)} />
                {sourceVideo && (
                  <small className="file-meta">{sourceVideo.name} ({formatFileSize(sourceVideo.size)})</small>
                )}
              </label>
            </div>

            <div className="submit-row">
              <button type="submit" className="primary-button" disabled={saving}>
                {saving ? '변환/저장 중...' : '저장'}
              </button>
              <span className="submit-hint">
                VOD는 파일 변환 후 저장되고, LIVE는 실시간 루프 스트림 프로세스가 시작됩니다.
              </span>
            </div>
          </form>

          {message && <p className="success-text">{message}</p>}
          {error && <p className="error-text">{error}</p>}
        </section>

        <section className="manage-list-section">
          <div className="section-headline">
            <h2>등록된 콘텐츠</h2>
            <span className="count-chip">총 {items.length}개</span>
          </div>

          {loading ? (
            <p>목록을 불러오는 중...</p>
          ) : (
            <div className="manage-card-grid">
              {items.map((item) => (
                <article key={item.id} className="manage-card">
                  <img src={getImageUrl(item.thumbnail)} alt={item.title} />
                  <div className="manage-card-body">
                    <h3>
                      {item.title}
                      <span className={`type-chip ${item.contentType}`}>{item.contentType.toUpperCase()}</span>
                    </h3>
                    <p>{item.description}</p>
                    <small>ID: {item.id}</small>
                    <small>HLS: {item.hlsPath}</small>
                    <small>분류: {getTypeLabel(item.contentType)}</small>
                    <small>길이: {item.duration || '-'}</small>
                    <small>해상도: {(item.availableResolutions || []).join(', ')}</small>
                    <div className="manage-card-actions">
                      <button
                        type="button"
                        className="danger-button"
                        onClick={() => handleDelete(item)}
                        disabled={deletingId === item.id}
                      >
                        {deletingId === item.id ? '삭제 중...' : '삭제'}
                      </button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

export default ContentManagePage

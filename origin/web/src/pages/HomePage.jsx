import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'
import { contentAPI, authAPI, browseAPI, getImageUrl } from '../services/api'
import './HomePage.css'

function HomePage() {
  const [contents, setContents] = useState([])
  const [featured, setFeatured] = useState(null)
  const [selectedType, setSelectedType] = useState('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const navigate = useNavigate()
  const { user, logout } = useAuthStore()

  useEffect(() => {
    loadContents()
  }, [])

  const loadContents = async () => {
    try {
      setLoading(true)
      const { data } = await contentAPI.getList()
      const allContents = Array.isArray(data) ? data : (data.contents || [])
      setContents(allContents)
      setFeatured(allContents.find(c => c.featured) || allContents[0])

      setLoading(false)
    } catch (error) {
      console.error('Failed to load contents:', error)
      setError(error.message)
      setLoading(false)
    }
  }

  const handleLogout = async () => {
    try {
      await authAPI.logout()
    } catch (err) {
      console.error('Logout error:', err)
    } finally {
      logout()
      navigate('/login')
    }
  }

  const handleWatch = async (content) => {
    try {
      await browseAPI.openContent(content.id)
    } catch (error) {
      console.warn('Failed to record browse event:', error)
    }

    navigate(`/watch/${content.id}`, { 
      state: { 
        content 
      } 
    })
  }

  const filteredContents = useMemo(() => {
    if (selectedType === 'all') return contents
    return contents.filter((item) => item.contentType === selectedType)
  }, [contents, selectedType])

  const groupedContents = useMemo(() => {
    return Object.entries(
      filteredContents.reduce((acc, content) => {
        const section = content.contentType === 'live' ? '라이브' : '콘텐츠'
        if (!acc[section]) acc[section] = []
        acc[section].push(content)
        return acc
      }, {})
    )
  }, [filteredContents])

  const featuredContent = useMemo(() => {
    if (!featured) return filteredContents[0] || null
    if (selectedType === 'all' || featured.contentType === selectedType) return featured
    return filteredContents[0] || null
  }, [featured, filteredContents, selectedType])

  const isAdmin = (user?.username || '').toLowerCase() === 'admin' || (user?.email || '').toLowerCase() === 'admin@ott.com'

  if (loading) {
    return (
      <div className="home-page">
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', color: 'white' }}>
          <h2>콘텐츠를 불러오는 중...</h2>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="home-page">
        <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', height: '100vh', color: 'white' }}>
          <h2>오류가 발생했습니다</h2>
          <p>{error}</p>
          <button onClick={loadContents} style={{ marginTop: '20px', padding: '10px 20px' }}>다시 시도</button>
        </div>
      </div>
    )
  }

  return (
    <div className="home-page">
      <header className="header">
        <div className="header-content">
          <h1 className="logo">OTTFLIX</h1>
          <nav className="header-nav">
            <button
              type="button"
              className={`nav-button ${selectedType === 'all' ? 'active' : ''}`}
              onClick={() => setSelectedType('all')}
            >
              홈
            </button>
            <button
              type="button"
              className={`nav-button ${selectedType === 'vod' ? 'active' : ''}`}
              onClick={() => setSelectedType('vod')}
            >
              콘텐츠
            </button>
            <button
              type="button"
              className={`nav-button ${selectedType === 'live' ? 'active' : ''}`}
              onClick={() => setSelectedType('live')}
            >
              라이브
            </button>
            {isAdmin && (
              <button type="button" className="nav-button" onClick={() => navigate('/manage')}>
                영상관리
              </button>
            )}
          </nav>
          <div className="header-right">
            <div className="user-menu">
              <div className="user-avatar">
                {user?.username?.charAt(0).toUpperCase() || 'U'}
              </div>
              <div className="user-dropdown">
                <div className="dropdown-item">
                  <span>{user?.username}</span>
                  <small>{user?.email}</small>
                </div>
                <div className="dropdown-divider"></div>
                {isAdmin && (
                  <button type="button" className="dropdown-item" onClick={() => navigate('/manage')}>
                    영상관리 이동
                  </button>
                )}
                <button onClick={handleLogout} className="dropdown-item">로그아웃</button>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="main-content">
        {featuredContent && (
          <section className="hero-banner">
            <div className="hero-backdrop" style={{ backgroundImage: `url(${getImageUrl(featuredContent.backdrop || featuredContent.thumbnail)})` }}>
              <div className="hero-content">
                <h2 className="hero-title">
                  {featuredContent.title}
                  {featuredContent.contentType === 'live' && <span className="live-badge">LIVE</span>}
                </h2>
                <p className="hero-description">{featuredContent.description}</p>
                <div className="hero-meta">
                  <span className="hero-year">{featuredContent.year || '-'}</span>
                  <span className="hero-rating">{featuredContent.rating || '-'}</span>
                  <span className="hero-duration">{featuredContent.duration || '-'}</span>
                  <span className="hero-duration">{(featuredContent.availableResolutions || []).join(', ')}</span>
                </div>
                <div className="hero-buttons">
                  <button className="btn-play" onClick={() => handleWatch(featuredContent)}>
                    ▶ 재생
                  </button>
                  {isAdmin && (
                    <button className="btn-info" onClick={() => navigate('/manage')}>
                      ⓘ 영상관리
                    </button>
                  )}
                </div>
              </div>
            </div>
          </section>
        )}

        {groupedContents.map(([category, categoryContents]) => (
          <section key={category} className="content-section">
            <h3 className="section-title">{category}</h3>
            <div className="content-row">
              {categoryContents.map((content) => (
                <div key={content.id} className="content-card" onClick={() => handleWatch(content)}>
                  <img src={getImageUrl(content.thumbnail)} alt={content.title} />
                  <div className="card-overlay">
                    <h4>
                      {content.title}
                      {content.contentType === 'live' && <span className="card-live-badge">LIVE</span>}
                    </h4>
                    <div className="card-info">
                      <span className="duration">{content.duration || '-'}</span>
                      <span className="rating">{content.rating || '-'}</span>
                      <span className="rating">{(content.availableResolutions || []).join(', ')}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        ))}
      </main>

      <footer className="footer">
        <p>© 2024 OTTFLIX. All rights reserved.</p>
      </footer>
    </div>
  )
}

export default HomePage

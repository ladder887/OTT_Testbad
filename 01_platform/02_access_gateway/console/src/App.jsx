import { useState, useEffect } from 'react'
import axios from 'axios'
import './App.css'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8081'

// URL 디코딩 헬퍼 함수
const decodeUsername = (username) => {
  if (!username || username === '-') return '-'
  try {
    return decodeURIComponent(username)
  } catch {
    return username
  }
}

function App() {
  const [stats, setStats] = useState(null)
  const [logs, setLogs] = useState([])
  const [blacklist, setBlacklist] = useState({ ips: [], tokens: [], referers: [] })
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState('dashboard')

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 5000) // 5초마다 갱신
    return () => clearInterval(interval)
  }, [])

  const fetchData = async () => {
    try {
      const [statsRes, logsRes, blacklistRes] = await Promise.all([
        axios.get(`${API_URL}/api/stats/blocked`),
        axios.get(`${API_URL}/api/logs/recent?size=50`),
        axios.get(`${API_URL}/api/blacklist`)
      ])
      
      setStats(statsRes.data)
      setLogs(logsRes.data.logs || [])
      setBlacklist(blacklistRes.data)
      setLoading(false)
    } catch (error) {
      console.error('Error fetching data:', error)
      setLoading(false)
    }
  }

  const addToBlacklist = async (type, value) => {
    try {
      await axios.post(`${API_URL}/api/blacklist`, { type, value })
      fetchData()
    } catch (error) {
      console.error('Error adding to blacklist:', error)
    }
  }

  const removeFromBlacklist = async (type, value) => {
    try {
      await axios.delete(`${API_URL}/api/blacklist`, { data: { type, value } })
      fetchData()
    } catch (error) {
      console.error('Error removing from blacklist:', error)
    }
  }

  if (loading) {
    return (
      <div className="app loading">
        <div className="loader">Loading...</div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Access Gateway Console</h1>
        <div className="header-info">
          <span className="status">● Connected</span>
        </div>
      </header>

      <nav className="tabs">
        <button 
          className={activeTab === 'dashboard' ? 'active' : ''}
          onClick={() => setActiveTab('dashboard')}
        >
          대시보드
        </button>
        <button 
          className={activeTab === 'logs' ? 'active' : ''}
          onClick={() => setActiveTab('logs')}
        >
          최근 요청
        </button>
        <button 
          className={activeTab === 'blacklist' ? 'active' : ''}
          onClick={() => setActiveTab('blacklist')}
        >
          블랙리스트
        </button>
      </nav>

      <main className="content">
        {activeTab === 'dashboard' && (
          <Dashboard stats={stats} />
        )}
        
        {activeTab === 'logs' && (
          <LogsView logs={logs} onBlock={addToBlacklist} />
        )}
        
        {activeTab === 'blacklist' && (
          <BlacklistView 
            blacklist={blacklist} 
            onAdd={addToBlacklist}
            onRemove={removeFromBlacklist}
          />
        )}
      </main>
    </div>
  )
}

function Dashboard({ stats }) {
  if (!stats) return <div>데이터 로딩 중...</div>

  const blockRate = stats.total > 0 ? ((stats.blocked / stats.total) * 100).toFixed(2) : 0

  return (
    <div className="dashboard">
      <div className="stats-grid">
        <div className="stat-card">
          <h3>전체 요청</h3>
          <div className="stat-value">{stats.total || 0}</div>
          <div className="stat-label">최근 1시간</div>
        </div>
        
        <div className="stat-card blocked">
          <h3>차단 요청</h3>
          <div className="stat-value">{stats.blocked || 0}</div>
          <div className="stat-label">차단율: {blockRate}%</div>
        </div>
        
        <div className="stat-card">
          <h3>허용 요청</h3>
          <div className="stat-value">{(stats.total || 0) - (stats.blocked || 0)}</div>
          <div className="stat-label">정상 트래픽</div>
        </div>
      </div>

      <div className="top-blocked">
        <h3>Top 차단 IP</h3>
        <table>
          <thead>
            <tr>
              <th>IP 주소</th>
              <th>요청 수</th>
            </tr>
          </thead>
          <tbody>
            {stats.top_ips && stats.top_ips.length > 0 ? (
              stats.top_ips.map((ip, idx) => (
                <tr key={idx}>
                  <td>{ip.key}</td>
                  <td>{ip.doc_count}</td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan="2">데이터 없음</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function LogsView({ logs, onBlock }) {
  const [filter, setFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [expandedRow, setExpandedRow] = useState(null);

  const filteredLogs = logs.filter(log => {
    const matchesSearch = !filter || 
      log.remote_addr?.includes(filter) ||
      log.client_ip?.includes(filter) ||
      log.request_uri?.includes(filter) ||
      log.http_user_agent?.includes(filter);
    
    const matchesStatus = statusFilter === 'all' || 
      (statusFilter === 'blocked' && log.blocked) ||
      (statusFilter === 'allowed' && !log.blocked) ||
      (statusFilter === '200' && log.status === 200) ||
      (statusFilter === '401' && log.status === 401) ||
      (statusFilter === '403' && log.status === 403);
    
    return matchesSearch && matchesStatus;
  });

  return (
    <div className="logs-view">
      <div className="logs-header">
        <h2>최근 요청 로그 ({filteredLogs.length}/{logs.length})</h2>
        <div className="logs-filters">
          <input 
            type="text" 
            placeholder="IP, URL, User-Agent 검색..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="filter-input"
          />
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="filter-select">
            <option value="all">전체</option>
            <option value="blocked">차단됨</option>
            <option value="allowed">허용됨</option>
            <option value="200">200 OK</option>
            <option value="401">401 Unauthorized</option>
            <option value="403">403 Forbidden</option>
          </select>
        </div>
      </div>
      <div className="table-container">
        <table>
          <thead>
            <tr>
              <th>시간</th>
              <th>클라이언트 IP</th>
              <th>게이트웨이 IP</th>
              <th>사용자</th>
              <th>세션 토큰</th>
              <th>메서드</th>
              <th>URL</th>
              <th>상태</th>
              <th>동작</th>
            </tr>
          </thead>
          <tbody>
            {filteredLogs.map((log, idx) => (
              <>
                <tr 
                  key={idx} 
                  className={`${log.blocked ? 'blocked-row' : ''} ${expandedRow === idx ? 'expanded' : ''}`}
                  onClick={() => setExpandedRow(expandedRow === idx ? null : idx)}
                >
                  <td>{new Date(log.timestamp).toLocaleString()}</td>
                  <td><strong>{log.client_ip || log.client_real_ip || log.remote_addr}</strong></td>
                  <td className="secondary-text">{log.remote_addr}</td>
                  <td className="user-cell">{decodeUsername(log.username || log.arg_user)}</td>
                  <td className="token-cell" title={log.session_token || log.arg_token}>
                    {(log.session_token || log.arg_token)?.substring(0, 12) || '-'}...
                  </td>
                  <td><span className="method-badge">{log.request_method || 'GET'}</span></td>
                  <td className="url-cell" title={log.request_uri}>{log.request_uri?.substring(0, 30)}...</td>
                  <td>
                    <span className={`status-badge status-${log.status}`}>
                      {log.status || 200}
                    </span>
                  </td>
                  <td>
                    <button 
                      className="btn-small btn-expand"
                      onClick={(e) => {
                        e.stopPropagation();
                        setExpandedRow(expandedRow === idx ? null : idx);
                      }}
                    >
                      {expandedRow === idx ? '▲' : '▼'}
                    </button>
                    {!log.blocked && (
                      <button 
                        className="btn-small btn-block"
                        onClick={(e) => {
                          e.stopPropagation();
                          onBlock('ips', log.client_ip || log.remote_addr);
                        }}
                      >
                        차단
                      </button>
                    )}
                  </td>
                </tr>
                {expandedRow === idx && (
                  <tr key={`${idx}-detail`} className="detail-row">
                    <td colSpan="8">
                      <div className="log-details">
                        <div className="detail-group">
                          <strong>사용자명:</strong> {decodeUsername(log.username || log.arg_user)}
                        </div>
                        <div className="detail-group">
                          <strong>세션 토큰:</strong> {log.session_token || log.arg_token || 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>Referer:</strong> {log.http_referer || 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>Content-ID:</strong> {log.content_id || log.arg_content_id || 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>사용자 ID:</strong> {log.user_id || 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>클라이언트 지문:</strong> {log.client_id || 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>전체 URL:</strong> {log.request_uri}
                        </div>
                        <div className="detail-group">
                          <strong>전체 User-Agent:</strong> {log.http_user_agent}
                        </div>
                        <div className="detail-group">
                          <strong>응답 시간:</strong> {log.request_time ? `${log.request_time}ms` : 'N/A'}
                        </div>
                        <div className="detail-group">
                          <strong>전송 바이트:</strong> {log.bytes_sent || 'N/A'}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function BlacklistView({ blacklist, onAdd, onRemove }) {
  const [newValue, setNewValue] = useState('')
  const [selectedType, setSelectedType] = useState('ips')

  const handleAdd = () => {
    if (newValue.trim()) {
      onAdd(selectedType, newValue.trim())
      setNewValue('')
    }
  }

  return (
    <div className="blacklist-view">
      <h2>블랙리스트 관리</h2>
      
      <div className="add-form">
        <select value={selectedType} onChange={(e) => setSelectedType(e.target.value)}>
          <option value="ips">IP 주소</option>
          <option value="tokens">토큰</option>
          <option value="referers">Referer</option>
        </select>
        
        <input
          type="text"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder="추가할 값 입력..."
        />
        
        <button onClick={handleAdd}>추가</button>
      </div>

      <div className="blacklist-sections">
        <div className="blacklist-section">
          <h3>차단된 IP ({blacklist.ips.length})</h3>
          <ul>
            {blacklist.ips.map((ip, idx) => (
              <li key={idx}>
                {ip}
                <button 
                  className="btn-remove"
                  onClick={() => onRemove('ips', ip)}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="blacklist-section">
          <h3>차단된 토큰 ({blacklist.tokens.length})</h3>
          <ul>
            {blacklist.tokens.map((token, idx) => (
              <li key={idx}>
                {token.substring(0, 30)}...
                <button 
                  className="btn-remove"
                  onClick={() => onRemove('tokens', token)}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="blacklist-section">
          <h3>차단된 Referer ({blacklist.referers.length})</h3>
          <ul>
            {blacklist.referers.map((ref, idx) => (
              <li key={idx}>
                {ref}
                <button 
                  className="btn-remove"
                  onClick={() => onRemove('referers', ref)}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}

export default App

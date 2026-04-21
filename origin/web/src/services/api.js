import axios from 'axios';

// API 요청을 게이트웨이(8081)로 보내 access.log 수집 경로에 포함한다.
const API_URL = import.meta.env.VITE_API_URL || `http://${window.location.hostname}:8081`;
const CDN_URL = import.meta.env.VITE_CDN_URL || 'http://192.168.0.111';

const api = axios.create({
  baseURL: API_URL,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json'
  }
});

// Request interceptor
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('accessToken');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const { data } = await axios.post(
          `${API_URL}/api/auth/refresh`,
          {},
          { withCredentials: true }
        );

        localStorage.setItem('accessToken', data.accessToken);
        localStorage.setItem('sessionToken', data.sessionToken);

        originalRequest.headers.Authorization = `Bearer ${data.accessToken}`;
        return api(originalRequest);
      } catch (refreshError) {
        // Refresh 실패 시 로그아웃
        localStorage.removeItem('accessToken');
        localStorage.removeItem('sessionToken');
        localStorage.removeItem('user');
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

// Auth API
export const authAPI = {
  register: (data) => api.post('/api/auth/register', data),
  login: (data) => api.post('/api/auth/login', data),
  logout: () => api.post('/api/auth/logout'),
  getMe: () => api.get('/api/auth/me'),
  refresh: () => api.post('/api/auth/refresh')
};

// User API
export const userAPI = {
  getProfile: () => api.get('/api/user/profile'),
  getWatchHistory: (params) => api.get('/api/user/watch-history', { params }),
  saveWatchHistory: (data) => api.post('/api/user/watch-history', data),
  getFavorites: () => api.get('/api/user/favorites'),
  addFavorite: (contentId) => api.post('/api/user/favorites', { content_id: contentId }),
  removeFavorite: (contentId) => api.delete(`/api/user/favorites/${contentId}`),
  getSessions: () => api.get('/api/user/sessions'),
  endSession: (sessionToken) => api.delete(`/api/user/sessions/${sessionToken}`)
};

// Content API
export const contentAPI = {
  getList: (params = {}) => api.get('/api/content/list', { params }),
  getDetail: (id) => api.get(`/api/content/${id}`),
  getRecommended: () => api.get('/api/content/recommended/for-you'),
  getTrending: () => api.get('/api/content/popular/trending'),
  getByGenre: (genre) => api.get(`/api/content/genre/${genre}`),
  getAdminList: () => api.get('/api/content/admin/list'),
  uploadManaged: (formData) =>
    api.post('/api/content/admin/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    }),
  deleteManaged: (contentId) => api.delete(`/api/content/admin/${encodeURIComponent(contentId)}`),
};

// Browse / Playback API
export const browseAPI = {
  search: (query) => api.get('/api/browse/search', { params: { q: query } }),
  openContent: (contentId) => api.get(`/api/browse/content/${contentId}`)
};

export const playbackAPI = {
  start: (contentId, metadata = {}) =>
    api.post('/api/playback/start', {
      content_id: contentId,
      ...(metadata || {}),
    })
};

// Admin API
export const adminAPI = {
  getUsers: (params) => api.get('/api/admin/users', { params }),
  getUserDetail: (userId) => api.get(`/api/admin/users/${userId}`),
  updateUserStatus: (userId, isActive) => api.patch(`/api/admin/users/${userId}/status`, { is_active: isActive }),
  getSessions: (params) => api.get('/api/admin/sessions', { params }),
  terminateSession: (sessionToken) => api.delete(`/api/admin/sessions/${sessionToken}`),
  getDashboardStats: () => api.get('/api/admin/stats/dashboard')
};

// 썸네일 URL 변환 헬퍼
export const getImageUrl = (path) => {
  if (!path) return '';
  if (path.startsWith('http')) return path;
  return `${CDN_URL}${path}`;
};

export default api;

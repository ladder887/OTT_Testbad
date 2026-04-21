const CONTENT_CATALOG = [
  {
    id: 'movie_001',
    hlsPath: 'cat1',
    title: '지구달이1',
    description: '지구달이 시리즈 1편',
    thumbnail: '/thumbnails/cat1.jpg',
    backdrop: '/thumbnails/cat1_backdrop.jpg',
    duration: '3분 29초',
    durationSec: 209,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '실증랩 VOD',
    contentType: 'vod',
    featured: true,
  },
  {
    id: 'movie_002',
    hlsPath: 'cat2',
    title: '지구달이2',
    description: '지구달이 시리즈 2편',
    thumbnail: '/thumbnails/cat2.jpg',
    backdrop: '/thumbnails/cat2_backdrop.jpg',
    duration: '3분 15초',
    durationSec: 195,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '실증랩 VOD',
    contentType: 'vod',
    featured: false,
  },
  {
    id: 'movie_003',
    hlsPath: 'cat3',
    title: '지구달이3',
    description: '지구달이 시리즈 3편',
    thumbnail: '/thumbnails/cat3.jpg',
    backdrop: '/thumbnails/cat3_backdrop.jpg',
    duration: '2분 45초',
    durationSec: 165,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '실증랩 VOD',
    contentType: 'vod',
    featured: false,
  },
  {
    id: 'movie_004',
    hlsPath: 'cat4',
    title: '지구달이4',
    description: '지구달이 시리즈 4편',
    thumbnail: '/thumbnails/cat4.jpg',
    backdrop: '/thumbnails/cat4_backdrop.jpg',
    duration: '4분 10초',
    durationSec: 250,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '실증랩 VOD',
    contentType: 'vod',
    featured: false,
  },
]

function getContentById(contentId) {
  return CONTENT_CATALOG.find((item) => item.id === contentId)
}

function searchContents(query) {
  if (!query || !query.trim()) {
    return CONTENT_CATALOG
  }

  const q = query.trim().toLowerCase()
  return CONTENT_CATALOG.filter((item) => {
    const haystack = [
      item.id,
      item.title,
      item.description,
      item.category,
      ...(item.genre || []),
    ]
      .join(' ')
      .toLowerCase()

    return haystack.includes(q)
  })
}

module.exports = {
  CONTENT_CATALOG,
  getContentById,
  searchContents,
}

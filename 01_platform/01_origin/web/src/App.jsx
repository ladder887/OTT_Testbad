import { Routes, Route } from 'react-router-dom'
import HomePage from './pages/HomePage'
import WatchPage from './pages/WatchPage'
import LoginPage from './pages/LoginPage'
import SignupPage from './pages/SignupPage'
import ContentManagePage from './pages/ContentManagePage'
import ProtectedRoute from './components/ProtectedRoute'
import './App.css'

function App() {
  return (
    <div className="app">
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route 
          path="/" 
          element={
            <ProtectedRoute>
              <HomePage />
            </ProtectedRoute>
          } 
        />
        <Route 
          path="/home" 
          element={
            <ProtectedRoute>
              <HomePage />
            </ProtectedRoute>
          } 
        />
        <Route 
          path="/watch/:contentId" 
          element={
            <ProtectedRoute>
              <WatchPage />
            </ProtectedRoute>
          } 
        />
        <Route
          path="/manage"
          element={
            <ProtectedRoute>
              <ContentManagePage />
            </ProtectedRoute>
          }
        />
      </Routes>
    </div>
  )
}

export default App

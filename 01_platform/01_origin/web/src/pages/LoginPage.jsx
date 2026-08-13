import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { authAPI } from '../services/api';
import { useAuthStore } from '../store/authStore';
import './LoginPage.css';

const LoginPage = () => {
  const navigate = useNavigate();
  const { login, setLoading } = useAuthStore();
  
  const [formData, setFormData] = useState({
    email: '',
    password: '',
    rememberMe: false
  });
  
  const [error, setError] = useState('');

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const { data } = await authAPI.login({
        email: formData.email,
        password: formData.password
      });

      login(data.user, data.accessToken, data.sessionToken);
      navigate('/home');
    } catch (err) {
      console.error('Login error:', err);
      let errorMessage = '로그인에 실패했습니다.';
      
      if (err.code === 'ERR_NETWORK') {
        errorMessage = '서버에 연결할 수 없습니다. Docker 컨테이너가 실행 중인지 확인하세요.';
      } else if (err.response?.data?.error) {
        errorMessage = err.response.data.error;
      }
      
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-header">
        <Link to="/" className="logo">OTTFLIX</Link>
      </div>
      
      <div className="login-content">
        <div className="login-body">
          <h1>로그인</h1>
          
          {error && (
            <div className="error-message">
              {error}
            </div>
          )}
          
          <form onSubmit={handleSubmit} className="login-form">
            <div className="form-group">
              <input
                type="email"
                name="email"
                placeholder="이메일 주소"
                value={formData.email}
                onChange={handleChange}
                required
              />
            </div>
            
            <div className="form-group">
              <input
                type="password"
                name="password"
                placeholder="비밀번호"
                value={formData.password}
                onChange={handleChange}
                required
              />
            </div>
            
            <button type="submit" className="btn-login">
              로그인
            </button>
            
            <div className="form-footer">
              <label className="remember-me">
                <input
                  type="checkbox"
                  name="rememberMe"
                  checked={formData.rememberMe}
                  onChange={handleChange}
                />
                <span>로그인 정보 저장</span>
              </label>
              
              <a href="#" className="help-link">도움이 필요하신가요?</a>
            </div>
          </form>
          
          <div className="signup-link">
            OTTFLIX 회원이 아닌가요? <Link to="/signup">지금 가입하세요.</Link>
          </div>
          
          <div className="recaptcha-info">
            이 페이지는 Google reCAPTCHA의 보호를 받아 로봇이 아님을 확인합니다.
          </div>
        </div>
      </div>
      
      <div className="login-footer">
        <div className="footer-content">
          <p>질문이 있으신가요? 문의 전화: 080-001-9587</p>
          <div className="footer-links">
            <a href="#">자주 묻는 질문</a>
            <a href="#">고객 센터</a>
            <a href="#">이용 약관</a>
            <a href="#">개인정보</a>
            <a href="#">쿠키 설정</a>
            <a href="#">회사 정보</a>
          </div>
        </div>
      </div>
    </div>
  );
};

export default LoginPage;

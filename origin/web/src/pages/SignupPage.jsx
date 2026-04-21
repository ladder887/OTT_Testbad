import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { authAPI } from '../services/api';
import { useAuthStore } from '../store/authStore';
import './SignupPage.css';

const SignupPage = () => {
  const navigate = useNavigate();
  const { login, setLoading } = useAuthStore();
  
  const [formData, setFormData] = useState({
    email: '',
    username: '',
    password: '',
    confirmPassword: ''
  });
  
  const [error, setError] = useState('');
  const [passwordStrength, setPasswordStrength] = useState(0);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));

    // Password strength check
    if (name === 'password') {
      let strength = 0;
      if (value.length >= 8) strength++;
      if (value.match(/[a-z]+/)) strength++;
      if (value.match(/[A-Z]+/)) strength++;
      if (value.match(/[0-9]+/)) strength++;
      if (value.match(/[!@#$%^&*]+/)) strength++;
      setPasswordStrength(strength);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    // Validation
    if (formData.password !== formData.confirmPassword) {
      setError('비밀번호가 일치하지 않습니다.');
      return;
    }

    if (formData.password.length < 6) {
      setError('비밀번호는 최소 6자 이상이어야 합니다.');
      return;
    }

    if (formData.username.length < 2) {
      setError('사용자명은 최소 2자 이상이어야 합니다.');
      return;
    }

    setLoading(true);

    try {
      const { data: registerData } = await authAPI.register({
        email: formData.email,
        username: formData.username,
        password: formData.password
      });

      // Auto login after registration
      const { data: loginData } = await authAPI.login({
        email: formData.email,
        password: formData.password
      });

      login(loginData.user, loginData.accessToken, loginData.sessionToken);
      navigate('/home');
    } catch (err) {
      console.error('Signup error:', err);
      let errorMessage = '회원가입에 실패했습니다.';
      
      if (err.code === 'ERR_NETWORK') {
        errorMessage = '서버에 연결할 수 없습니다. Docker 컨테이너가 실행 중인지 확인하세요.';
      } else if (err.response?.data?.error) {
        errorMessage = err.response.data.error;
      } else if (err.response?.data?.errors?.[0]?.msg) {
        errorMessage = err.response.data.errors[0].msg;
      }
      
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const getStrengthLabel = () => {
    const labels = ['매우 약함', '약함', '보통', '강함', '매우 강함'];
    return labels[passwordStrength - 1] || '';
  };

  const getStrengthColor = () => {
    const colors = ['#e50914', '#ff6b00', '#ffc107', '#4caf50', '#0d8b3a'];
    return colors[passwordStrength - 1] || '#666';
  };

  return (
    <div className="signup-page">
      <div className="signup-header">
        <Link to="/" className="logo">OTTFLIX</Link>
        <Link to="/login" className="signin-link">로그인</Link>
      </div>
      
      <div className="signup-content">
        <div className="signup-body">
          <h1>회원가입</h1>
          <p className="subtitle">언제 어디서나 시청하세요</p>
          
          {error && (
            <div className="error-message">
              {error}
            </div>
          )}
          
          <form onSubmit={handleSubmit} className="signup-form">
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
                type="text"
                name="username"
                placeholder="사용자 이름 (최소 2자)"
                value={formData.username}
                onChange={handleChange}
                required
                minLength="2"
              />
            </div>
            
            <div className="form-group">
              <input
                type="password"
                name="password"
                placeholder="비밀번호 (최소 6자)"
                value={formData.password}
                onChange={handleChange}
                required
                minLength="6"
              />
              {formData.password && (
                <div className="password-strength">
                  <div className="strength-bar">
                    <div 
                      className="strength-fill" 
                      style={{ 
                        width: `${passwordStrength * 20}%`,
                        backgroundColor: getStrengthColor()
                      }}
                    />
                  </div>
                  <span style={{ color: getStrengthColor() }}>
                    {getStrengthLabel()}
                  </span>
                </div>
              )}
            </div>
            
            <div className="form-group">
              <input
                type="password"
                name="confirmPassword"
                placeholder="비밀번호 확인"
                value={formData.confirmPassword}
                onChange={handleChange}
                required
              />
            </div>
            
            <button type="submit" className="btn-signup">
              가입하기
            </button>
          </form>
          
          <div className="terms">
            <p>가입하면 OTTFLIX의 <a href="#">이용 약관</a> 및 <a href="#">개인정보 처리방침</a>에 동의하는 것으로 간주됩니다.</p>
          </div>
          
          <div className="login-link">
            이미 계정이 있으신가요? <Link to="/login">로그인하세요.</Link>
          </div>
        </div>
      </div>
      
      <div className="signup-footer">
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

export default SignupPage;

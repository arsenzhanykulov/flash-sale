import { Link, Navigate, Route, Routes } from 'react-router-dom'

import { useAuth } from './auth/authContext'
import { LoginPage } from './auth/LoginPage'
import { SalePage } from './sales/SalePage'
import { SalesListPage } from './sales/SalesListPage'

function Header() {
  const { user, logout } = useAuth()

  return (
    <header className="header">
      <Link to="/" className="header__brand">
        Flash&nbsp;Sale
      </Link>
      <nav className="header__nav">
        {user === null ? (
          <Link to="/login" className="button button--ghost">
            Войти
          </Link>
        ) : (
          <>
            <span className="header__user">{user.email}</span>
            <button type="button" className="button button--ghost" onClick={logout}>
              Выйти
            </button>
          </>
        )}
      </nav>
    </header>
  )
}

export function App() {
  return (
    <div className="app">
      <Header />
      <main className="main">
        <Routes>
          <Route path="/" element={<SalesListPage />} />
          <Route path="/sales/:saleId" element={<SalePage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

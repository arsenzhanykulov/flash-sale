import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import { AuthProvider } from './auth/AuthProvider'
import { ServerClockProvider } from './time/ServerClockProvider'
import './index.css'

const container = document.getElementById('root')
if (container === null) throw new Error('Не найден элемент #root')

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ServerClockProvider>
          <App />
        </ServerClockProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)

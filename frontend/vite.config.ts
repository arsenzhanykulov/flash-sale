import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    // Контейнеру нужно слушать все интерфейсы, иначе dev-сервер
    // недоступен с хоста.
    host: true,
    port: 5173,
    watch: {
      // Код приходит через смонтированный том: на macOS и Windows
      // события файловой системы до контейнера не доходят.
      usePolling: true,
      interval: 300,
    },
  },
})

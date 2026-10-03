import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时前端跑在 5173，/api 代理到 FastAPI（默认 8000），避免跨域配置。
// 构建产物直接输出到 ../web/static —— FastAPI 检测到该目录存在就会自动托管，
// 因此生产环境只需要跑一个进程。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
        // SSE 必须关闭代理缓冲，否则事件会被攒着一起发
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache'
          })
        },
      },
    },
  },
  build: {
    outDir: '../web/static',
    emptyOutDir: true,
    sourcemap: false,
  },
})

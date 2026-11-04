/**
 * ESLint 扁平配置。
 *
 * 只做「找错」，不做「排版」—— 项目里的格式已经统一，引入 prettier 会把
 * 所有文件重排一遍，产生几千行无关 diff，把真实改动埋掉。格式统一留到
 * 需要的时候单独做一次。
 *
 * 规则取 recommended，不启用 type-aware（需要 program 信息，跑得慢、
 * 且会和 tsc --noEmit 的检查大量重叠）。
 */

import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'eslint.config.js'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // 未使用变量交给 tsc 的 noUnusedLocals 管，这里只管「真的写错了」的情况
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],

      // 下面两条来自 React Compiler 的规则集（eslint-plugin-react-hooks v6），
      // 对 React 18 项目过严，关掉：
      //
      // - set-state-in-effect：在 effect 里同步 setState 是常见且合法的模式
      //   （例如根据 props 初始化本地状态、在挂载时读一次 DOM）。规则本意是
      //   防止级联渲染，但把这个模式一律判错，误报远多于真问题。
      // - refs：本项目多处用「render 期间把最新值写进 ref」保证事件回调里
      //   拿到最新值（React 官方也承认这个模式，只是推荐 19+ 用 useEffectEvent）。
      //   要彻底消除它得把 React 升到 19，那是另一件事。
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/refs': 'off',
    },
  },
)

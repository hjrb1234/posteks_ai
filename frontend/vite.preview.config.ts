import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  base: './',
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  build: {
    outDir: 'preview-dist', emptyOutDir: true, cssCodeSplit: false, minify: 'esbuild',
    lib: {entry: 'preview/main.tsx', name: 'PosteksPreview', formats: ['iife'], fileName: () => 'preview.js'},
    rollupOptions: {output: {inlineDynamicImports: true}},
  },
});

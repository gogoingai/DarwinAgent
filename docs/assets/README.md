# Documentation graphics / 文档配图

The four visual groups are bilingual. Technical diagrams are rendered from their actual Mermaid sources; the hero's editable SVG is its source. No fonts are embedded and no media is hosted remotely.

四组图均有中英文版本。技术图由对应 Mermaid 图源实际导出；头图以可编辑 SVG 为图源。不嵌入字体，不上传外部图床。

| Visual group | English source / exports | 中文图源／导出 |
| --- | --- | --- |
| Hero | [SVG source](hero-en.svg) · [PNG](hero-en.png) | [SVG 图源](hero-zh-CN.svg) · [PNG](hero-zh-CN.png) |
| Architecture | [Mermaid](architecture-en.mmd) · [SVG](architecture-en.svg) · [PNG](architecture-en.png) | [Mermaid](architecture-zh-CN.mmd) · [SVG](architecture-zh-CN.svg) · [PNG](architecture-zh-CN.png) |
| Evolution loop | [Mermaid](evolution-loop-en.mmd) · [SVG](evolution-loop-en.svg) · [PNG](evolution-loop-en.png) | [Mermaid](evolution-loop-zh-CN.mmd) · [SVG](evolution-loop-zh-CN.svg) · [PNG](evolution-loop-zh-CN.png) |
| Asset boundary | [Mermaid](asset-boundary-en.mmd) · [SVG](asset-boundary-en.svg) · [PNG](asset-boundary-en.png) | [Mermaid](asset-boundary-zh-CN.mmd) · [SVG](asset-boundary-zh-CN.svg) · [PNG](asset-boundary-zh-CN.png) |

## Rebuild locally

Requires Node.js 22.13+ and npm. Install a local Chromium-capable Mermaid runtime and sharp, then run the portable renderer:

```bash
cd docs/assets
npm ci
npm run build
```

The committed lockfile freezes transitive dependencies. Pinned direct tools: `@mermaid-js/mermaid-cli@12.0.0`, `sharp@0.34.5`. Chromium must be available to Puppeteer; normal npm installation provisions its browser unless downloads are disabled. To select a local browser, provide a Puppeteer configuration JSON via `PUPPETEER_CONFIG`. To use an already installed CLI, set `MMDC` to its executable. Neither option needs a hardcoded machine path.

`build.mjs` exports every `.mmd` to SVG and PNG with the shared palette/portable font stack in `mermaid-config.json`. Mermaid uses `--no-font-embed`; no proprietary font bytes are copied into SVG. Install a CJK font such as Noto Sans CJK SC for Chinese rendering. PNG exports are rendered at scale 2; hero rasterization uses density 192. PNGs are suitable for article/WeChat use and require no reader-side fonts. SVG uses portable system font fallbacks.

The branching hero is illustrative. Its branches do not represent a measured population search or genetic algorithm. The loop diagram describes the general asset proposal boundary; the packaged demo changes P only. Architecture depicts the shared runtime and independent evaluator, not arbitrary external agent plugins.

# Changelog

## [0.3.1](https://github.com/longsizhuo/openInvest/compare/invest-setup-skill-v0.3.0...invest-setup-skill-v0.3.1) (2026-07-06)


### Docs

* **skill:** SKILL.md 补 Hermes 原生元数据（platforms + metadata.hermes.tags）——增量字段，Claude/Codex 忽略 ([fa69fd7](https://github.com/longsizhuo/openInvest/commit/fa69fd79db744a6fdbf68fb42bd174098c06275a))

## [0.3.0](https://github.com/longsizhuo/openInvest/compare/invest-setup-skill-v0.2.0...invest-setup-skill-v0.3.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **plugin:** skill 源文件 git 路径变更 skills/* → plugin/skills/*（根 skills/ 符号链接保持磁盘兼容）

### Features

* **plugin:** Codex plugin cache 瘦身 44MB→156KB——真身入 plugin/，marketplace source 指回 ./plugin ([c3ad092](https://github.com/longsizhuo/openInvest/commit/c3ad0929960309afc90b0d822d8a0ad9d55c6ed4))

## [0.2.0](https://github.com/longsizhuo/openInvest/compare/invest-setup-skill-v0.1.1...invest-setup-skill-v0.2.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **gui:** run.sh gui 子命令移除；web_api 不再挂载 GUI 静态文件

### Refactor

* **gui:** GUI 壳层退役——后端不再 serve 静态文件，Web API 标记 deprecated ([390c87d](https://github.com/longsizhuo/openInvest/commit/390c87d6c43775d03abe3dfd42df10bf74cc1679))

## [0.1.1](https://github.com/longsizhuo/openInvest/compare/invest-setup-skill-v0.1.0...invest-setup-skill-v0.1.1) (2026-06-13)


### Docs

* **setup-skill:** 新增'连接已有 hub'onboarding 路径 ([7ef9b8b](https://github.com/longsizhuo/openInvest/commit/7ef9b8b6bb8baf5222d320c508b35e458bfa1065))

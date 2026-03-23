# AGENTS.md

本项目是七牛云 AIGC 设计场景解决方案验证项目（DesignCraft Agent）。

## 项目结构

```
├── frontend/          # Vue 3 前端应用
├── backend/           # Python LangChain 后端
├── skills/            # AI Agent SKILLs 定义目录
└── docs/              # 文档
```

## 技术栈

**前端：** Vue 3 + TypeScript + TDesign Chat + AG-UI + Vite  
**后端：** Python + LangChain + FastAPI  
**搜索：** SerpAPI + Bing Search APIs  
**生图：** 七牛云大模型服务

## 构建与运行

### 前端 (frontend/)

```bash
npm install
npm run dev          # 开发服务器
npm run build        # 生产构建
npm run lint         # ESLint 检查
npm run type-check   # TypeScript 类型检查
npm run test         # 单元测试
npm run test:watch   # 测试监听模式
npm run test:e2e     # E2E 测试
```

### 后端 (backend/)

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload          # 开发服务器
python -m pytest tests/                          # 运行全部测试
python -m pytest tests/test_xxx.py -v            # 运行单个测试文件
python -m pytest tests/test_xxx.py::test_func    # 运行单个测试函数
python -m mypy app/                              # 类型检查
python -m ruff check app/                        # 代码检查
python -m ruff format app/                       # 代码格式化
```

## SKILLs 规范

SKILLs 存放在 `./backend/design-skills/<name>/SKILL.md`，必须包含 YAML frontmatter：

注意：**这里的 SKILLs 是为 AI 设计 Agent 使用的，并不属于 Opencode、Codex。**

```yaml
---
name: image-edit
description: 编辑和调整已有图片，支持裁剪、滤镜、风格转换等操作
---
```

命名规则：`^[a-z0-9]+(-[a-z0-9]+)*$`  
描述长度：1-1024 字符，需具体以便代理正确选择。

## 代码风格

### 通用规范

- **语言：** 所有代码注释、API 消息、文档使用中文
- **命名：**
  - 变量/函数：camelCase（前端）/ camelCase（后端）
  - 类型/接口：PascalCase
  - 常量：UPPER_SNAKE_CASE
  - 文件名：kebab-case

### 前端 (Vue 3 + TypeScript)

- 使用 `<script setup>` 语法 + TypeScript
- 组件文件：`MyComponent.vue`
- 组合式函数：`useFeature.ts`
- 导入顺序：第三方库 → 内部模块 → 相对路径
- 使用 Pinia 管理状态
- 路由使用 Vue Router

### 后端 (Python)

- 导入顺序：标准库 → 第三方库 → 本地模块（isort 排序）
- 类型注解：所有函数签名必须包含类型注解
- API 路由使用 FastAPI 路由器
- 异步函数使用 `async/await`

### 错误处理

- **前端：** 捕获错误后展示友好提示，记录到监控
- **后端：** 使用 FastAPI 异常处理器，返回标准错误响应格式
- **代理执行：** 永远不要静默吞掉异常

## 工作流约定

1. 创建新功能前先读取相关 SKILL.md
2. 遵循现有代码模式，不要擅自引入新依赖
3. 提交前运行 lint 和 type-check
4. 测试覆盖率保持在关键路径 80%+

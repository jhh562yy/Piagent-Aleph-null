# pi-ai-base

本地优先的 AI Agent 基座：一个真实可用的本地代理（PI Studio），外加一个河流中心线提取算法模块。
只依赖标准库，开箱即用；模型请求、命令执行、文件读写全部发生在你自己机器上。

## 主要文件

| 文件 / 目录 | 说明 |
|---|---|
| `app/piengine.py` | 引擎：OpenAI 兼容协议 + SSE 流式 + 工具调用循环 + 检查点/回滚 + 上下文账本 |
| `app/picore.py` | 核心能力层：记忆 / 技能库 / MCP / 调度 / 错误知识库 / 子代理 |
| `app/picontract.py` | 契约系统：目标 + 验收项 + 守卫检查 + 结算报告 |
| `app/piplan.py` | 计划与编排（步骤清单 + 子代理编排） |
| `app/pipolicy.py` | 审批策略：工具 × 正则 → allow / deny / ask |
| `app/piapi.py` | 自定义 HTTP 接口，注册成模型可调用的工具 |
| `app/piplugins.py` | 本地插件基座（声明式 `plugin.json`） |
| `app/pistudio.py` | 原生桌面窗口（tkinter） |
| `app/piserver.py` | 本地 HTTP 后端：REST + SSE 流式对话 + 审批/问答往返 |
| `app/web/index.html` | 网页版单页界面（后端 `/classic` 提供） |
| `workbench.html` | 网页工作台（停靠式布局，后端 `/` 提供） |
| `river-centerline/` | 断裂河流修复 + 中心线提取：算法、自测、可视化网页模板 |
| `requirements.txt` | `river-centerline/` 需要 numpy、Pillow；`app/` 无需任何 pip 包 |
| `config.example.json` | `~/.pistudio/config.json` 的模板（自己填 API Key） |

## 运行

```bat
app\PiStudio.cmd        :: 原生桌面窗口
app\PiStudio-Web.cmd    :: 网页版（起本地后端 + 开浏览器）
app\PiStudio-Debug.cmd  :: 带控制台，先跑自检
```

配置写进 `~/.pistudio/config.json`（照 `config.example.json` 填）。
三个启动器优先使用 `runtime/python/` 里的便携 Python，找不到才回退系统 Python（该运行时体积大，未随仓库提供）。

## 河流中心线提取

```bat
cd river-centerline
python test_predict.py   :: 单元自测
python run_check.py      :: 合成场景 + 精度指标
python build_demo.py     :: 生成可视化网页（river-centerline-viz.html）
```

算法细节、实测指标与调参见 `river-centerline/README.md`。

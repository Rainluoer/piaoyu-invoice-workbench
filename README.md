# 票屿 Piaoyu

本地发票管理工作台：扫描和导入发票、核对信息、记录报销状态、处理重复票据，生成科研材料入库单。浏览器界面和 SQLite 数据库都运行在自己的电脑上，日常使用不需要账号、API Key 或云服务。

## 功能

- 导入 PDF、PNG、JPG 和 ZIP；支持指定发票文件夹、子目录扫描和目录排除。
- 按票号或文件内容识别重复项，统计不重复计入金额；跨页全选、筛选、排序及搜索。
- 手动修改票据信息；发票类型通过固定下拉选择，记录变更前后及时间。
- 批量标记待报销或已报销；删除有二次确认，文件进入可恢复的本地回收站。
- 导出所选原件 ZIP、CSV 台账及数据库备份。
- 科研材料 PDF 一键生成入库单；识别失败后可补填或直接生成，允许缺失字段留空；草稿可恢复。
- 入库单列表只显示本工作台生成的成品，保留最近五份；项目名称和代码保留两行空白，供使用者填写。
- 四种界面风格一键切换并记忆，文字至少 14px。

## 下载和启动

主要面向 Windows 10/11。需要 Python **3.10 或以上**，安装时勾选 **Add Python to PATH**。Node.js 仅用于前端开发测试，日常使用不需要。

1. 点击仓库页面 **Code → Download ZIP**，解压到普通文件夹；也可以用 Git 克隆。
2. 首次使用双击 **安装依赖.cmd**。它在项目内创建 `.venv`，从 PyPI 安装依赖，需要联网。可使用已有兼容虚拟环境跳过此步。
3. 双击 **启动工作台.cmd**，浏览器自动打开 `http://127.0.0.1:18626/`。启动窗口保持运行。
4. 将票据放进项目的 `invoices` 文件夹，再点击扫描；或进入 **本地设置 → 发票目录** 指定已有文件夹，也可直接导入。

完成安装后，管理发票和生成入库单可离线运行。不需要 Codex、Word 或单独安装 ruku 技能；入库单脚本已经随项目附带。

终端启动方法：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app\server.py
```

需要换端口或排除目录时：

```powershell
.venv\Scripts\python.exe app\server.py --port 18627 --ignore-dir "D:\MyInvoices\generated"
```

`--ignore-dir` 可重复指定，每次启动需传入。也可给启动脚本传入相同参数。服务只监听 `127.0.0.1`，不用于公网部署。退出可关闭启动窗口或在本地设置中点击“停止服务”。

macOS/Linux 可以安装依赖后运行 `python app/server.py`，在设置中手动粘贴目录路径；Windows 文件夹选择与“打开目录”按钮属于 Windows 功能。此项目的桌面启动脚本主要在 Windows 验证。

## 数据在哪里

| 内容 | 默认位置 |
| --- | --- |
| 初始发票目录 | `invoices/`，可在设置中更改 |
| 数据库和修改记录 | `app/data/invoices.sqlite3` |
| 导入副本与预览 | `app/data/imports/`、`app/data/previews/` |
| 回收站 | `app/data/trash/` |
| 入库单任务、草稿及成品 | `app/data/receipts/` |

切换发票目录保留旧台账和原票位置。报销状态变更只写入台账；用户确认删除后，所选原件才移到回收站。生成入库单使用独立副本，保留最近五份生成结果；不扫描或列出用户原来已有的其他 DOCX，也不改动这些文件。

升级前备份 `app/data/` 及自己的发票目录，再替换程序源码。数据库备份不包含所有原始票据和生成文件，完整备份请保留这些目录。Git 忽略规则会排除本地数据，但提交前仍应查看 `git status`，不要使用 `git add -f` 强行加入票据。

## 识别和入库单

自动识别读取 PDF 文字层；图片和扫描件没有自动 OCR，需要手动填写。提取失败可展开“补填发票信息”，默认显示品名、型号（规格）、单位、数量和不含税金额，其余字段选填。所有字段为空也允许继续生成，未知内容在 Word 中留空。

金额合计、数字格式、文件来源与文件变化仍会检查。单价接受有效非负高精度数字；单商品采用票面价税合计，多商品在金额和税额完整时核对合计。成品采用浙江大学科研材料入库单模板，使用者应按自己的单位要求核对，并填写项目及签字栏。替换模板前请阅读 [架构说明](docs/ARCHITECTURE.md)。

## 一起改进

欢迎提交 Issue 和 Pull Request。推荐流程是 **Fork → 创建分支 → 修改和测试 → 向本仓库提交 Pull Request**；维护者审阅后合并。详细步骤见 [贡献指南](CONTRIBUTING.md)。不要在 Issue、PR 或截图中附上真实发票和本地数据库。

```powershell
python -m pip install -r requirements-dev.txt
cd app
python -m unittest discover -p "test_*.py" -v
node test_ui_filters.cjs
node test_themes.cjs
node test_receipt_polling.cjs
```

项目采用 Python 标准库 HTTP 服务、SQLite 和原生 HTML/CSS/JavaScript，无前端构建步骤。测试使用隔离临时目录和合成数据。GitHub Actions 对提交和 Pull Request 自动运行回归。

## 开源许可和来源

项目采用 [MIT License](LICENSE)。入库单处理使用 [3450530868/Ruku](https://github.com/3450530868/Ruku) 的三个 MIT 许可脚本，保留原始代码和版权声明；具体版本和 SHA256 见 [第三方说明](THIRD_PARTY_NOTICES.md)。空白模板基于该项目模板整理，保留所需两行空段，公开副本已清除作者和 WPS 元数据。

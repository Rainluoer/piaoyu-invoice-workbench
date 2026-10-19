# 第三方组件

## Ruku

- 来源：https://github.com/3450530868/Ruku
- 上游版本：`c6fcc63dd375d866fdb8dfa40cdfd752d0a13ec4`
- 作者版权：Copyright (c) 2026 3450530868
- 许可：MIT，完整文本位于 `app/vendor/ruku/LICENSE`。

`app/vendor/ruku/scripts/` 中 extract_invoices.py、rename_files.py、fill_receipt.py 为原始脚本，逐个 SHA256 记录在同目录上一级的 UPSTREAM.json。票屿的任务隔离、手动补填、空字段兼容和版式整理在工作台适配层实现。

`app/assets/入库单模板.docx` 基于 Ruku 空白模板整理：移除预设项目文字，保留两行空白项目区，去掉文末多余空段，并清除公开副本的文档属性；其他模板结构保留。它适用于对应表格格式，使用者应核对所在单位的实际要求。

## Python 依赖

运行所需的 pdfplumber、python-docx、pypdfium2、lxml、Pillow 通过 PyPI 安装，各自适用其发布版本的许可；本仓库不捆绑这些库的二进制发行包。reportlab 仅用于开发测试。前端不加载第三方 CDN 或字体。

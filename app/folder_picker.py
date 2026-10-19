"""应用内按需打开本机文件夹选择窗口，不自动更改设置。"""
import base64
import os
import subprocess


def pick_folder(initial):
    if os.name != 'nt':
        raise ValueError('请在路径框中粘贴文件夹的完整路径')
    encoded_path = base64.b64encode(str(initial).encode('utf-8')).decode('ascii')
    # 路径使用 base64 传递，避免引号或特殊字符变成 PowerShell 代码。
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
$taskPicker = New-Object System.Windows.Forms.FolderBrowserDialog
$taskPicker.Description = '选择发票文件夹'
$taskPicker.ShowNewFolderButton = $false
$taskPicker.SelectedPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
$taskOwner = New-Object System.Windows.Forms.Form
$taskOwner.TopMost = $true
$taskOwner.ShowInTaskbar = $false
$taskOwner.Opacity = 0
try {
    $taskOwner.Show()
    if ($taskPicker.ShowDialog($taskOwner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($taskPicker.SelectedPath))
    }
} finally {
    $taskPicker.Dispose()
    $taskOwner.Dispose()
}
'''.replace('__PATH__', encoded_path)
    command = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', command], capture_output=True, text=True, encoding='ascii', errors='replace', timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError('文件夹选择已超时，请重试或直接粘贴路径') from None
    if result.returncode:
        raise ValueError('无法打开文件夹选择窗口，请直接粘贴完整路径')
    output = result.stdout.strip()
    if not output:
        return {'cancelled': True, 'path': ''}
    try:
        return {'cancelled': False, 'path': base64.b64decode(output, validate=True).decode('utf-8')}
    except (ValueError, UnicodeError):
        raise ValueError('未能读取选择的路径，请直接粘贴完整路径') from None

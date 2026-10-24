Option Explicit
Dim shell, files, folder, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & files.BuildPath(folder, "scripts\start.ps1") & Chr(34)
result = shell.Run(command, 0, True)
If result <> 0 Then
    shell.Popup "Startup failed. Run the CMD launcher to see the error, or check app\data\server-error.log.", 0, "Piaoyu", 16
End If

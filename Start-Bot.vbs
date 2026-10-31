Option Explicit
Dim shell, files, root, exe, python, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
exe = root & "\runtime\desktop\CompanionBot\CompanionBot.exe"
python = root & "\.venv\Scripts\pythonw.exe"
If files.FileExists(exe) Then
    command = Chr(34) & exe & Chr(34)
Else
    command = Chr(34) & python & Chr(34) & " " & Chr(34) & root & "\desktop_entry.py" & Chr(34)
End If
shell.Run command, 1, False

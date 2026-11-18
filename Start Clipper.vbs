' Starts Clipper without opening a console window.
' (Electron is a windowed app; launched from a console it would stay tied to it and close with it.)
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
On Error Resume Next   ' the variable is usually not set; removing a missing one raises an error
shell.Environment("Process").Remove("ELECTRON_RUN_AS_NODE")
On Error GoTo 0
shell.Run """" & folder & "\node_modules\electron\dist\electron.exe"" """ & folder & """", 1, False

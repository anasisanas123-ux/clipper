' Starts Clipper without opening a console window.
' (Electron is a windowed app; launched from a console it would stay tied to it and close with it.)
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
electron = folder & "\node_modules\electron\dist\electron.exe"
python = folder & "\.venv\Scripts\python.exe"

If Not fso.FileExists(electron) And fso.FileExists(folder & "\node_modules\electron\install.js") Then
  ' The app window's download didn't finish during setup: fetch it now (shows a window while it downloads).
  shell.Run "cmd /c title Clipper: downloading the app window && node """ & folder & "\node_modules\electron\install.js""", 1, True
End If

If Not fso.FileExists(electron) Or Not fso.FileExists(python) Then
  MsgBox "Clipper isn't fully installed on this PC (missing: " & _
    IIf(fso.FileExists(electron), "", "the app window ") & IIf(fso.FileExists(python), "", "the Python engine") & ")." & vbCrLf & vbCrLf & _
    "Run ""Install Clipper.bat"" again with the internet on. It repairs the install and keeps your settings.", _
    vbExclamation, "Clipper"
  WScript.Quit 1
End If

On Error Resume Next   ' the variable is usually not set; removing a missing one raises an error
shell.Environment("Process").Remove("ELECTRON_RUN_AS_NODE")
On Error GoTo 0
shell.Run """" & electron & """ """ & folder & """", 1, False

Function IIf(cond, a, b)
  If cond Then IIf = a Else IIf = b
End Function

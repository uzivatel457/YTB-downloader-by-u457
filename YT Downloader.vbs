' Spusti YT Downloader bez konzoloveho okna (dvojklik).
Option Explicit
Dim sh, fso, root, pyw, py, logFile
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

logFile = root & "\data\launch.log"
If Not fso.FolderExists(root & "\data") Then fso.CreateFolder(root & "\data")

pyw = root & "\.venv\Scripts\pythonw.exe"
py = root & "\.venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
  ' Prvni spusteni — nabidni instalaci
  Dim answer
  answer = MsgBox( _
    "YT Downloader jeste neni nainstalovan." & vbCrLf & vbCrLf & _
    "Spustit instalaci vsech zavislosti?" & vbCrLf & _
    "(Python, Node.js, ffmpeg, balicky)", _
    vbYesNo + vbQuestion, "YT Downloader — instalace" _
  )
  If answer = vbYes Then
    sh.Run "cmd /c """ & root & "\install.bat""", 1, True
  Else
    sh.Run "cmd /c """ & root & "\setup.bat""", 1, True
  End If
  If Not fso.FileExists(py) Then WScript.Quit 1
End If

Dim exe
If fso.FileExists(pyw) Then
  exe = """" & pyw & """ """ & root & "\main.py"""
Else
  exe = """" & py & """ """ & root & "\main.py"""
End If

On Error Resume Next
Dim ts
Set ts = fso.OpenTextFile(logFile, 8, True)
ts.WriteLine Now & " | VBS launch"
ts.Close
On Error GoTo 0

sh.Run exe, 0, False

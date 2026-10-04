Option Explicit

Dim shell, fso, scriptPath, runner
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "bo3_workshop_downloader.py")

' Hide the console host itself. Python then owns the Tk window normally.
runner = "py.exe -3 """ & scriptPath & """"

On Error Resume Next
shell.Run runner, 0, False

If Err.Number <> 0 Then
    MsgBox "Could not start BO3 Workshop Downloader." & vbCrLf & _
           "Python launcher: py.exe" & vbCrLf & _
           "Error: " & Err.Description, vbCritical + vbOKOnly, _
           "BO3 Workshop Downloader"
End If

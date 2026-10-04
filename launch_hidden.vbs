Option Explicit

Dim shell, fso, scriptPath, pythonw, execResult, line, logPath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "bo3_workshop_downloader.py")
logPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "launcher.log")

pythonw = ""

On Error Resume Next
Set execResult = shell.Exec("cmd.exe /d /c where pyw.exe")
If Err.Number = 0 Then
    If Not execResult.StdOut.AtEndOfStream Then
        line = Trim(execResult.StdOut.ReadLine())
        If line <> "" Then pythonw = line
    End If
End If
Err.Clear

If pythonw = "" Then
    Set execResult = shell.Exec("cmd.exe /d /c where pythonw.exe")
    If Err.Number = 0 Then
        If Not execResult.StdOut.AtEndOfStream Then
            line = Trim(execResult.StdOut.ReadLine())
            If line <> "" Then pythonw = line
        End If
    End If
End If
Err.Clear
On Error GoTo 0

If pythonw = "" Then
    Dim lf
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Could not find pyw.exe or pythonw.exe on PATH."
    lf.Close
    MsgBox "Python windowless interpreter was not found." & vbCrLf & _
           "See launcher.log for details.", vbCritical, "BO3 Workshop Downloader"
    WScript.Quit 1
End If

On Error Resume Next
shell.Run """" & pythonw & """ """ & scriptPath & """", 0, False
If Err.Number <> 0 Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Start failed: " & Err.Description
    lf.Close
    MsgBox "Could not start the downloader." & vbCrLf & _
           "See launcher.log for details.", vbCritical, "BO3 Workshop Downloader"
End If
On Error GoTo 0

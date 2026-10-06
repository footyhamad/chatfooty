Option Explicit

Dim shell, fso, scriptDir, scriptPath, interpreter, logPath, pathValue, parts, i, candidate, lf, sep
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

sep = Chr(92)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(scriptDir, "bo3_workshop_downloader.py")
logPath = fso.BuildPath(scriptDir, "launcher.log")
interpreter = ""

' Find a real windowless Python executable directly from PATH.
' Do not invoke cmd.exe/where: that causes console flashes.
On Error Resume Next
pathValue = shell.Environment("Process")("PATH")
On Error GoTo 0

If pathValue <> "" Then
    parts = Split(pathValue, ";")
    For i = 0 To UBound(parts)
        candidate = Trim(parts(i))
        If candidate <> "" Then
            Do While Right(candidate, 1) = sep
                candidate = Left(candidate, Len(candidate) - 1)
            Loop

            If fso.FileExists(candidate & sep & "pythonw.exe") Then
                interpreter = candidate & sep & "pythonw.exe"
                Exit For
            End If

            If fso.FileExists(candidate & sep & "pyw.exe") Then
                interpreter = candidate & sep & "pyw.exe"
                Exit For
            End If
        End If
    Next
End If

If interpreter = "" Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - No pythonw.exe/pyw.exe found on PATH."
    lf.WriteLine Now & " - PATH=" & pathValue
    lf.Close
    MsgBox "Python's windowless launcher (pythonw.exe/pyw.exe) was not found." & vbCrLf & _
           "Open launcher.log for the detected PATH.", vbCritical, "BO3 Workshop Downloader"
    WScript.Quit 1
End If

If Not fso.FileExists(scriptPath) Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Script not found: " & scriptPath
    lf.Close
    MsgBox "Downloader script was not found:" & vbCrLf & _
           scriptPath, vbCritical, "BO3 Workshop Downloader"
    WScript.Quit 1
End If

Set lf = fso.OpenTextFile(logPath, 8, True)
lf.WriteLine Now & " - Launching with: " & interpreter
lf.WriteLine Now & " - Script: " & scriptPath
lf.Close

On Error Resume Next
Err.Clear
shell.Run """" & interpreter & """ """ & scriptPath & """", 0, False
If Err.Number <> 0 Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Start failed: " & Err.Number & " - " & Err.Description
    lf.Close
    MsgBox "Could not start the downloader." & vbCrLf & _
           "See launcher.log for details.", vbCritical, "BO3 Workshop Downloader"
End If
On Error GoTo 0

Option Explicit

Dim shell, fso, scriptDir, scriptPath, interpreter, logPath, pathValue, parts, i, candidate, lf
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(scriptDir, "bo3_workshop_downloader.py")
logPath = fso.BuildPath(scriptDir, "launcher.log")
interpreter = ""

' Find pythonw.exe/pyw.exe directly from PATH. Do NOT invoke cmd.exe/where:
' that was causing visible console flashes on some Windows setups.
On Error Resume Next
pathValue = shell.Environment("Process")("PATH")
On Error GoTo 0

If pathValue <> "" Then
    parts = Split(pathValue, ";")
    For i = 0 To UBound(parts)
        candidate = Trim(parts(i))
        If candidate <> "" Then
            If Right(candidate, 1) = "" Then
                candidate = Left(candidate, Len(candidate) - 1)
            End If

            If fso.FileExists(candidate & "pythonw.exe") Then
                interpreter = candidate & "pythonw.exe"
                Exit For
            End If

            If fso.FileExists(candidate & "pyw.exe") Then
                interpreter = candidate & "pyw.exe"
                Exit For
            End If
        End If
    Next
End If

' Microsoft Store Python can expose python.exe through WindowsApps without a
' pythonw alias being present. Locate python.exe in PATH and launch it hidden
' as a last resort.
If interpreter = "" And pathValue <> "" Then
    parts = Split(pathValue, ";")
    For i = 0 To UBound(parts)
        candidate = Trim(parts(i))
        If candidate <> "" Then
            If Right(candidate, 1) = "" Then
                candidate = Left(candidate, Len(candidate) - 1)
            End If
            If fso.FileExists(candidate & "python.exe") Then
                interpreter = candidate & "python.exe"
                Exit For
            End If
        End If
    Next
End If

If interpreter = "" Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Could not find pythonw.exe, pyw.exe, or python.exe on PATH."
    lf.Close
    MsgBox "Python could not be found." & vbCrLf & _
           "See launcher.log for details.", vbCritical, "BO3 Workshop Downloader"
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

On Error Resume Next
shell.Run """" & interpreter & """ """ & scriptPath & """", 0, False
If Err.Number <> 0 Then
    Set lf = fso.OpenTextFile(logPath, 8, True)
    lf.WriteLine Now & " - Start failed using " & interpreter & ": " & Err.Description
    lf.Close
    MsgBox "Could not start the downloader." & vbCrLf & _
           "See launcher.log for details.", vbCritical, "BO3 Workshop Downloader"
End If
On Error GoTo 0

Option Explicit

Dim shell, fso, scriptDir, pyw, execResult, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Find the real windowless Python interpreter instead of assuming pyw.exe exists.
On Error Resume Next
Set execResult = shell.Exec("cmd /c where pythonw.exe")
If Err.Number = 0 Then
    pyw = Trim(execResult.StdOut.ReadLine())
End If
Err.Clear
On Error GoTo 0

If pyw = "" Then
    pyw = "pyw.exe"
End If

cmd = """" & pyw & """ """ & fso.BuildPath(scriptDir, "bo3_workshop_downloader.py") & """"
On Error Resume Next
shell.Run cmd, 0, False

If Err.Number <> 0 Then
    Dim answer
    answer = MsgBox("Could not start BO3 Workshop Downloader." & vbCrLf & _
                    "Python windowless interpreter: " & pyw & vbCrLf & _
                    "Error: " & Err.Description & vbCrLf & vbCrLf & _
                    "Try running the Python script directly to see the error.", _
                    vbCritical + vbOKOnly, "BO3 Workshop Downloader")
End If

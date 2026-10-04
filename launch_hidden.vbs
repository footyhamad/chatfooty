Set shell = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run "pyw.exe """ & scriptDir & "bo3_workshop_downloader.py" & """", 0, False

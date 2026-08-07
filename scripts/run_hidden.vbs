' Launches run_bot.ps1 with no console window.
'
' Why a VBScript wrapper: Task Scheduler's "-WindowStyle Hidden" still creates a console
' host for powershell.exe, so a black window flashes at logon and (with some Windows
' builds) stays on screen. WScript.Shell.Run with intWindowStyle = 0 creates the process
' with no window at all. wscript.exe itself is windowless.
'
' bWaitOnReturn = True is deliberate: this process stays alive while the bot runs, so the
' scheduled task keeps the state "Running" and scripts/stop_bot.ps1 can find and kill the
' whole tree. With False the task would report success immediately and lose the handle.
'
' ASCII only, on purpose: Windows Script Host reads .vbs as ANSI, and Cyrillic comments
' turn into mojibake that breaks parsing - the same trap as .ps1 files without a BOM.
' Russian explanation lives in README.md and CLAUDE.md.

Option Explicit

Dim shell, fso, scriptDir, runner, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
runner = fso.BuildPath(scriptDir, "run_bot.ps1")

If Not fso.FileExists(runner) Then
    WScript.Quit 2
End If

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & runner & """"

WScript.Quit shell.Run(command, 0, True)

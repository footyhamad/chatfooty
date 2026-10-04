# BO3 Workshop Downloader

A Windows-friendly GUI wrapper around SteamCMD for Call of Duty: Black Ops III Workshop downloads (AppID 311210).

## Why this exists

SteamCMD can fail large Workshop downloads with "Timeout downloading item" after several minutes. This application treats that failure as resumable: it never deletes SteamCMD's partial Workshop download and simply launches the same command again.

The GUI reports progress independently by summing the actual files under SteamCMD's Workshop download directory, so a restart can show the bytes already present.

## Features

- BO3 AppID 311210
- SteamCMD only; no custom Steam CDN implementation
- Automatic retry/resume after timeout or failure
- Never clears partial Workshop download data on retry
- Real progress, speed, and ETA from the filesystem
- Workshop metadata lookup (title and expected size)
- Multiple Workshop IDs in one queue
- Optional export/copy to a custom output directory
- Persistent settings
- Per-item SteamCMD logs
- Windows GUI using stdlib Tkinter

## Run

1. Install Python 3.10+ with Tkinter.
2. Install SteamCMD and log in once with the Steam account that owns BO3.
3. Run run.bat or py bo3_workshop_downloader.py.
4. Select your steamcmd.exe, enter the cached Steam account, choose an output directory, and enter Workshop IDs.
5. Press START / RESUME.

Native SteamCMD partials live under:
<steamcmd>\steamapps\workshop\downloads\311210\<workshop_id>

Completed Workshop content lives under:
<steamcmd>\steamapps\workshop\content\311210\<workshop_id>

## LuckyFaraday retry model

The retry behavior follows the proven pattern used by luckeyfaraday/bo3-workshop-ps4: invoke

steamcmd +login <user> +workshop_download_item 311210 <id> validate +quit

again after a timeout while preserving the existing partial Workshop directory.

## Build a Windows executable

Run:
powershell -ExecutionPolicy Bypass -File build_windows.ps1

This uses PyInstaller and emits a single-file GUI executable in dist.

## License

MIT

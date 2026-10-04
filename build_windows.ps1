$ErrorActionPreference = "Stop"
py -m pip install --upgrade pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name BO3WorkshopDownloader bo3_workshop_downloader.py
Write-Host "Built dist\BO3WorkshopDownloader.exe"

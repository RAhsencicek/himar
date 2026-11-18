; ==========================================================
; Hİ MARKET KURUMSAL POS & STOK OTOMASYONU - INNO SETUP SCRIPTI
; Windows 10 / 11 64-bit Tek Tıkla Kurulum Paketi
; ==========================================================

#define MyAppName "HiMarket POS"
#define MyAppVersion "2.6.0"
#define MyAppPublisher "HiMarket Perakende Otomasyonu"
#define MyAppURL "https://github.com/AlperenGoral/HiMarket"
#define MyAppExeName "kiosk_launcher.py"

[Setup]
AppId={{E6B23F10-4A3C-4D2A-98C3-72C5B4E99812}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName=C:\HiMarket
DefaultGroupName=HiMarket
AllowNoIcons=yes
OutputDir=..\installer_output
OutputBaseFilename=HiMarket_POS_Kurulum_v2.6.0
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Bilgisayar açıldığında Kasa Satış Terminalini otomatik başlat"; GroupDescription: "Başlangıç Ayarları:"

[Files]
Source: "..\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".git\*,.venv\*,__pycache__\*,backups\printed_receipts\*"

[Icons]
Name: "{group}\{#MyAppName} Kasa Satış"; Filename: "{app}\BASLAT.bat"; IconFilename: "{app}\HiMarket.icns"
Name: "{group}\{#MyAppName} Yönetim Paneli"; Filename: "http://127.0.0.1:8000/management/dashboard/"
Name: "{group}\{#MyAppName} Veritabanı Yedeği Al"; Filename: "{app}\\.venv\\Scripts\\python.exe"; Parameters: "manage.py backup_database --compress --keep 30 --reason MANUEL"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName} Kasa Satış"; Filename: "{app}\BASLAT.bat"; IconFilename: "{app}\HiMarket.icns"; Tasks: desktopicon
Name: "{autodesktop}\{#MyAppName} Yönetim Paneli"; Filename: "http://127.0.0.1:8000/management/dashboard/"; Tasks: desktopicon

[Registry]
; Windows Başlangıcında Otomatik Başlatma (Kiosk Modu)
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "HiMarketPOS"; ValueData: """{app}\BASLAT.bat"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Kurulum tamamlandığında güvenlik duvarı port izni ver (Port 8000)
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""HiMarket POS Server"" dir=in action=allow protocol=TCP localport=8000"; Flags: runhidden
; İlk kurulum paketlerini ve veritabanı migrasyonlarını çalıştır
Filename: "{app}\BASLAT.bat"; Description: "{#MyAppName} uygulamasını şimdi başlat"; Flags: nowait postinstall skipifsilent


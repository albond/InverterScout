@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "PROJECT_NAME=inverterscout"
set "DEFAULT_WEB_PORT=8080"
set "DEFAULT_INVERTER_PORT=8000"
for %%I in ("%~dp0.") do set "ROOT_DIR=%%~fI"

cd /d "%ROOT_DIR%" || (
  echo Error: Cannot open the InverterScout directory.
  exit /b 1
)

if /I "%~1"=="--help" goto :help
if /I "%~1"=="-h" goto :help
if /I "%~1"=="--self-test" goto :selfTest
if not "%~1"=="" (
  echo Error: Unknown option: %~1
  exit /b 1
)

echo InverterScout Quick Start
echo ==========================
echo   1. Docker on this computer
echo   2. Home NAS or Docker-capable Arduino Linux board over SSH
echo   3. Remote Linux server over SSH
echo      Ordinary Arduino boards such as Uno, Nano, and Mega cannot run Docker.

:chooseTarget
set "DEPLOYMENT_MODE="
set /p "DEPLOYMENT_MODE=Choose deployment target [1]: "
if not defined DEPLOYMENT_MODE set "DEPLOYMENT_MODE=1"
if "!DEPLOYMENT_MODE!"=="1" goto :localDeployment
if "!DEPLOYMENT_MODE!"=="2" (
  set "REMOTE_KIND=home_remote"
  goto :remoteDeployment
)
if "!DEPLOYMENT_MODE!"=="3" (
  set "REMOTE_KIND=remote"
  goto :remoteDeployment
)
echo Enter 1, 2, or 3.
goto :chooseTarget

:localDeployment
call :ensureLocalDocker || exit /b 1
call :selectBindAddress local || exit /b 1
call :selectLocalWebPort || exit /b 1
if not "%BIND_ADDRESS%"=="127.0.0.1" call :configureWindowsLanFirewall
call :collectInverterTarget || exit /b 1
call :writeLocalEnv || exit /b 1

echo.
echo Validating and building InverterScout...
docker compose -p "%PROJECT_NAME%" config --quiet || exit /b 1
docker compose -p "%PROJECT_NAME%" build inverterscout || exit /b 1

call :askYesNo "Test inverter reachability from the container now?" Y
if not errorlevel 1 (
  docker compose -p "%PROJECT_NAME%" run --rm --no-deps --entrypoint python inverterscout -c "import socket,sys; connection=socket.create_connection((sys.argv[1], int(sys.argv[2])), 5); connection.close()" "%INVERTER_HOST%" "%INVERTER_PORT%"
  if errorlevel 1 (
    echo The container cannot reach %INVERTER_HOST%:%INVERTER_PORT%.
    echo Check the address, VLAN/firewall rules, and routing from the Docker host.
    call :askYesNo "Start the Web UI anyway?" N
    if errorlevel 1 (
      echo Deployment stopped before the application was started.
      exit /b 1
    )
  ) else (
    echo Inverter TCP connection succeeded.
  )
) else (
  echo Inverter connectivity test skipped. Verify it in the first-run wizard.
)

docker compose -p "%PROJECT_NAME%" up -d --no-build || exit /b 1
call :waitForService
if errorlevel 1 (
  docker compose -p "%PROJECT_NAME%" logs --tail 80 inverterscout
  echo Error: InverterScout did not become healthy.
  exit /b 1
)

echo.
echo InverterScout is running.
if "%BIND_ADDRESS%"=="127.0.0.1" (
  set "WEB_URL=http://localhost:%WEB_PORT%"
) else (
  set "WEB_URL=http://%BIND_ADDRESS%:%WEB_PORT%"
)
echo Open: %WEB_URL%
if not "%BIND_ADDRESS%"=="127.0.0.1" call :showLanAccessNotes "%BIND_ADDRESS%" "%WEB_PORT%"
echo Complete the browser wizard. Re-enter the inverter address there so it is stored in the encrypted database.
call :askYesNo "Open the setup wizard now?" Y
if not errorlevel 1 start "" "%WEB_URL%"
exit /b 0

:remoteDeployment
call :ensureRemoteTools || exit /b 1
call :collectRemoteAccess || exit /b 1
call :selectBindAddress %REMOTE_KIND% || exit /b 1
call :selectRemoteWebPort || exit /b 1
call :collectInverterTarget || exit /b 1

set "PROBE_MODE=skip"
call :askYesNo "Require a successful inverter connection from the remote container?" Y
if not errorlevel 1 set "PROBE_MODE=required"

for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')"') do set "RELEASE_ID=%%I-%RANDOM%"
set "BUNDLE_PATH=%TEMP%\inverterscout-bundle-%RANDOM%-%RANDOM%.tar.gz"
set "OUTPUT_PATH=%TEMP%\inverterscout-remote-%RANDOM%-%RANDOM%.log"
set "REMOTE_ARCHIVE=/tmp/inverterscout-%RELEASE_ID%.tar.gz"
set "DESTINATION=%SSH_USER%@%SSH_HOST%"

set "COPYFILE_DISABLE=1"
tar --no-xattrs -czf "%BUNDLE_PATH%" -C "%ROOT_DIR%" .dockerignore Dockerfile LICENSE README.md docker-compose.yml pyproject.toml src
set "COPYFILE_DISABLE="
if errorlevel 1 (
  echo Error: Could not create the deployment bundle.
  call :cleanupRemoteFiles
  exit /b 1
)

echo.
echo Uploading a runtime-only source bundle over SSH...
echo OpenSSH may ask for the host-key confirmation, account password, or key passphrase.
if defined SSH_KEY_PATH (
  scp -P "%SSH_PORT%" -i "%SSH_KEY_PATH%" -o IdentitiesOnly=yes "%BUNDLE_PATH%" "%DESTINATION%:%REMOTE_ARCHIVE%"
) else (
  scp -P "%SSH_PORT%" "%BUNDLE_PATH%" "%DESTINATION%:%REMOTE_ARCHIVE%"
)
if errorlevel 1 (
  echo Error: Secure upload failed.
  call :cleanupRemoteFiles
  exit /b 1
)

echo Checking remote Docker, selecting ports, building, and starting InverterScout...
if defined SSH_KEY_PATH (
  ssh -p "%SSH_PORT%" -i "%SSH_KEY_PATH%" -o IdentitiesOnly=yes "%DESTINATION%" "bash -s -- '%REMOTE_ARCHIVE%' '%BIND_ADDRESS%' '%WEB_PORT%' '%INVERTER_HOST%' '%INVERTER_PORT%' '%PROBE_MODE%' '%RELEASE_ID%'" < "%ROOT_DIR%\scripts\deployment\remote-install.sh" > "%OUTPUT_PATH%"
) else (
  ssh -p "%SSH_PORT%" "%DESTINATION%" "bash -s -- '%REMOTE_ARCHIVE%' '%BIND_ADDRESS%' '%WEB_PORT%' '%INVERTER_HOST%' '%INVERTER_PORT%' '%PROBE_MODE%' '%RELEASE_ID%'" < "%ROOT_DIR%\scripts\deployment\remote-install.sh" > "%OUTPUT_PATH%"
)
set "REMOTE_STATUS=%ERRORLEVEL%"
if exist "%OUTPUT_PATH%" type "%OUTPUT_PATH%"
if not "%REMOTE_STATUS%"=="0" (
  echo Error: Remote deployment failed. No SSH password or private key was saved.
  call :cleanupRemoteFiles
  exit /b 1
)

set "SELECTED_REMOTE_PORT="
for /f "tokens=2 delims==" %%I in ('findstr /b "INVERTERSCOUT_WEB_PORT=" "%OUTPUT_PATH%"') do set "SELECTED_REMOTE_PORT=%%I"
call :validatePort SELECTED_REMOTE_PORT
if errorlevel 1 (
  echo Error: The remote installer did not return a valid Web UI port.
  call :cleanupRemoteFiles
  exit /b 1
)

echo.
if "%BIND_ADDRESS%"=="127.0.0.1" (
  set "TUNNEL_PORT=%SELECTED_REMOTE_PORT%"
  call :portInUse TUNNEL_PORT
  if not errorlevel 1 call :findFreePort TUNNEL_PORT TUNNEL_PORT
  if not defined TUNNEL_PORT (
    echo Error: No free local port was found for the SSH tunnel.
    call :cleanupRemoteFiles
    exit /b 1
  )
  echo Keep this command running while using the Web UI:
  if defined SSH_KEY_PATH (
    echo ssh -N -L !TUNNEL_PORT!:127.0.0.1:%SELECTED_REMOTE_PORT% -p %SSH_PORT% -i "%SSH_KEY_PATH%" %DESTINATION%
  ) else (
    echo ssh -N -L !TUNNEL_PORT!:127.0.0.1:%SELECTED_REMOTE_PORT% -p %SSH_PORT% %DESTINATION%
  )
  echo Then open: http://localhost:!TUNNEL_PORT!
) else (
  call :showLanAccessNotes "%BIND_ADDRESS%" "%SELECTED_REMOTE_PORT%"
)
echo Complete the browser wizard. Re-enter the inverter address there so it is stored in the encrypted database.
call :cleanupRemoteFiles
exit /b 0

:ensureLocalDocker
where docker >nul 2>&1 || (
  echo Error: Docker was not found. Install Docker Desktop, then run start.bat again.
  exit /b 1
)
docker compose version >nul 2>&1 || (
  echo Error: The Docker Compose v2 plugin is missing.
  exit /b 1
)
docker info >nul 2>&1 && exit /b 0

if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
  call :askYesNo "Docker Desktop is not running. Start it now?" Y
  if errorlevel 1 exit /b 1
  start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
  echo Waiting for Docker Desktop...
  call :waitForDocker
  if not errorlevel 1 exit /b 0
)
echo Error: The Docker daemon is not available. Start Docker Desktop and run this launcher again.
exit /b 1

:waitForDocker
for /L %%I in (1,1,60) do (
  docker info >nul 2>&1 && exit /b 0
  timeout /t 2 /nobreak >nul
)
exit /b 1

:ensureRemoteTools
where ssh >nul 2>&1 || (
  echo Error: OpenSSH client ^(ssh^) was not found. Enable the Windows OpenSSH Client feature.
  exit /b 1
)
where scp >nul 2>&1 || (
  echo Error: OpenSSH secure copy ^(scp^) was not found.
  exit /b 1
)
where tar >nul 2>&1 || (
  echo Error: tar was not found. Install the standard Windows archive tools.
  exit /b 1
)
where powershell >nul 2>&1 || (
  echo Error: Windows PowerShell was not found.
  exit /b 1
)
exit /b 0

:selectBindAddress
set "BIND_KIND=%~1"
set "SUGGESTED_BIND_ADDRESS="
set "DEFAULT_BIND_CHOICE=1"
if /I "%BIND_KIND%"=="local" call :detectLocalLanIPv4
if /I "%BIND_KIND%"=="home_remote" (
  set "SUGGESTED_BIND_ADDRESS=%SSH_HOST%"
  call :validatePrivateIPv4 SUGGESTED_BIND_ADDRESS
  if errorlevel 1 set "SUGGESTED_BIND_ADDRESS="
  set "DEFAULT_BIND_CHOICE=2"
)
echo Who should be able to open the Web UI?
if /I "%BIND_KIND%"=="remote" (
  echo   1. This computer through an SSH tunnel only ^(recommended^)
) else if /I "%BIND_KIND%"=="home_remote" (
  echo   1. This computer through an SSH tunnel only
) else (
  echo   1. Only this computer at localhost ^(recommended^)
)
if /I "%BIND_KIND%"=="remote" (
  echo   2. Devices on the remote host's trusted private LAN or VPN
) else if /I "%BIND_KIND%"=="home_remote" (
  echo   2. Other devices on the trusted home LAN ^(recommended for a NAS or Arduino Linux board^)
) else (
  echo   2. Other devices on this trusted home LAN
)
:chooseBind
set "BIND_CHOICE="
set /p "BIND_CHOICE=Choose Web UI access [!DEFAULT_BIND_CHOICE!]: "
if not defined BIND_CHOICE set "BIND_CHOICE=!DEFAULT_BIND_CHOICE!"
if "!BIND_CHOICE!"=="1" (
  set "BIND_ADDRESS=127.0.0.1"
  exit /b 0
)
if not "!BIND_CHOICE!"=="2" (
  echo Enter 1 or 2.
  goto :chooseBind
)
:enterBindAddress
set "BIND_ADDRESS="
echo LAN access publishes the selected Docker port on one private host address.
echo It does not and must not create public router port forwarding.
if defined SUGGESTED_BIND_ADDRESS (
  set /p "BIND_ADDRESS=Docker host private LAN IPv4 address [!SUGGESTED_BIND_ADDRESS!]: "
  if not defined BIND_ADDRESS set "BIND_ADDRESS=!SUGGESTED_BIND_ADDRESS!"
) else (
  set /p "BIND_ADDRESS=Docker host private LAN IPv4 address: "
)
call :validatePrivateIPv4 BIND_ADDRESS
if errorlevel 1 (
  echo Enter a private IPv4 address such as 192.168.x.x, 10.x.x.x, or 172.16-31.x.x.
  goto :enterBindAddress
)
if /I "%BIND_KIND%"=="local" (
  call :addressBelongsToLocalHost BIND_ADDRESS
  if errorlevel 1 (
    echo Address %BIND_ADDRESS% is not assigned to this computer.
    goto :enterBindAddress
  )
)
exit /b 0

:detectLocalLanIPv4
set "SUGGESTED_BIND_ADDRESS="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$route=Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric,InterfaceMetric | Select-Object -First 1; if ($route) { $addresses=Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue; foreach($address in $addresses) { $ip=$address.IPAddress; if ($ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') { Write-Output $ip; exit } } }"`) do set "SUGGESTED_BIND_ADDRESS=%%I"
exit /b 0

:configureWindowsLanFirewall
echo Windows Firewall may block connections from other devices even when Docker publishes the port.
echo The optional rule is limited to the Private network profile, this computer's selected IP, and LocalSubnet.
call :askYesNo "Add or refresh this scoped Windows Firewall rule for TCP port %WEB_PORT%?" Y
if errorlevel 1 (
  echo Firewall unchanged. If LAN access fails, allow TCP port %WEB_PORT% only from the trusted local subnet.
  exit /b 0
)
powershell -NoProfile -Command "$name='InverterScout Web UI TCP '+$env:WEB_PORT; $rule=Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue; if ($rule) { $rule | Remove-NetFirewallRule }; New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $env:WEB_PORT -LocalAddress $env:BIND_ADDRESS -RemoteAddress LocalSubnet -Profile Private | Out-Null"
if errorlevel 1 (
  echo Warning: Windows could not create the firewall rule. Run start.bat as Administrator or add the scoped rule manually.
  echo Keep the home network profile set to Private. The Docker deployment will continue.
) else (
  echo Windows Firewall allows TCP port %WEB_PORT% from LocalSubnet on the Private profile.
)
exit /b 0

:showLanAccessNotes
echo Other devices on the trusted LAN can use: http://%~1:%~2
echo If another device cannot connect, allow inbound TCP port %~2 from the local subnet in the host firewall.
echo Never forward this Web UI port on the internet router; use Telegram or a private VPN away from home.
exit /b 0

:selectLocalWebPort
:enterLocalWebPort
set "WEB_PORT="
set /p "WEB_PORT=Web UI port [%DEFAULT_WEB_PORT%]: "
if not defined WEB_PORT set "WEB_PORT=%DEFAULT_WEB_PORT%"
call :validatePort WEB_PORT
if errorlevel 1 (
  echo Enter a number from 1 to 65535.
  goto :enterLocalWebPort
)
call :portInUse WEB_PORT
if errorlevel 1 exit /b 0
call :portOwnedByProject WEB_PORT
if not errorlevel 1 exit /b 0
call :findFreePort WEB_PORT SUGGESTED_PORT
if not defined SUGGESTED_PORT (
  echo Port %WEB_PORT% is occupied and no nearby free port was found.
  goto :enterLocalWebPort
)
echo Port %WEB_PORT% is occupied. Suggested free port: %SUGGESTED_PORT%.
call :askYesNo "Use port %SUGGESTED_PORT%?" Y
if errorlevel 1 goto :enterLocalWebPort
set "WEB_PORT=%SUGGESTED_PORT%"
exit /b 0

:selectRemoteWebPort
:enterRemoteWebPort
set "WEB_PORT="
set /p "WEB_PORT=Preferred remote Web UI port [%DEFAULT_WEB_PORT%]: "
if not defined WEB_PORT set "WEB_PORT=%DEFAULT_WEB_PORT%"
call :validatePort WEB_PORT
if errorlevel 1 (
  echo Enter a number from 1 to 65535.
  goto :enterRemoteWebPort
)
exit /b 0

:collectInverterTarget
:enterInverterHost
set "INVERTER_HOST="
set /p "INVERTER_HOST=Inverter hostname or IPv4 address: "
call :validateHost INVERTER_HOST
if errorlevel 1 (
  echo Use a hostname or IPv4 address without a URL or path.
  goto :enterInverterHost
)
:enterInverterPort
set "INVERTER_PORT="
set /p "INVERTER_PORT=Inverter TCP port [%DEFAULT_INVERTER_PORT%]: "
if not defined INVERTER_PORT set "INVERTER_PORT=%DEFAULT_INVERTER_PORT%"
call :validatePort INVERTER_PORT
if errorlevel 1 (
  echo Enter a number from 1 to 65535.
  goto :enterInverterPort
)
exit /b 0

:collectRemoteAccess
:enterSshHost
set "SSH_HOST="
set /p "SSH_HOST=Remote Docker host (IPv4 address or DNS name): "
call :validateHost SSH_HOST
if errorlevel 1 (
  echo Use an IPv4 address or DNS name without a URL or path.
  goto :enterSshHost
)
:enterSshPort
set "SSH_PORT="
set /p "SSH_PORT=SSH port [22]: "
if not defined SSH_PORT set "SSH_PORT=22"
call :validatePort SSH_PORT
if errorlevel 1 (
  echo Enter a number from 1 to 65535.
  goto :enterSshPort
)
:enterSshUser
set "SSH_USER="
set /p "SSH_USER=SSH username: "
call :validateUsername SSH_USER
if errorlevel 1 (
  echo Use a normal Linux username without spaces or shell characters.
  goto :enterSshUser
)
echo SSH authentication:
echo   1. Password ^(entered securely by OpenSSH and never stored^)
echo   2. Private key
:chooseSshAuth
set "SSH_AUTH="
set /p "SSH_AUTH=Choose authentication [1]: "
if not defined SSH_AUTH set "SSH_AUTH=1"
if "!SSH_AUTH!"=="1" (
  set "SSH_KEY_PATH="
  exit /b 0
)
if not "!SSH_AUTH!"=="2" (
  echo Enter 1 or 2.
  goto :chooseSshAuth
)
:enterSshKey
set "SSH_KEY_PATH="
set /p "SSH_KEY_PATH=Path to the private key: "
if not exist "%SSH_KEY_PATH%" (
  echo Private key not found: %SSH_KEY_PATH%
  goto :enterSshKey
)
exit /b 0

:writeLocalEnv
(
  echo INVERTERSCOUT_BIND_ADDRESS=%BIND_ADDRESS%
  echo INVERTERSCOUT_WEB_PORT=%WEB_PORT%
) > ".env"
if errorlevel 1 (
  echo Error: Could not write .env.
  exit /b 1
)
exit /b 0

:waitForService
powershell -NoProfile -Command "$id=(docker compose -p '%PROJECT_NAME%' ps -a -q inverterscout | Select-Object -First 1); if (-not $id) { exit 1 }; foreach ($attempt in 1..45) { $state=(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $id 2>$null); if ($state -in @('healthy','running')) { exit 0 }; if ($state -in @('unhealthy','exited','dead')) { exit 1 }; Start-Sleep -Seconds 2 }; exit 1"
exit /b %ERRORLEVEL%

:validatePort
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "$value=0; if ([int]::TryParse($env:CHECK_VALUE, [ref]$value) -and $value -ge 1 -and $value -le 65535) { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:validateHost
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "if ($env:CHECK_VALUE -match '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$') { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:validateUsername
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "if ($env:CHECK_VALUE -match '^[A-Za-z_][A-Za-z0-9._-]*$') { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:validateIPv4
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "$address=$null; if ($env:CHECK_VALUE -match '^(\d{1,3}\.){3}\d{1,3}$' -and [Net.IPAddress]::TryParse($env:CHECK_VALUE, [ref]$address) -and $address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:validatePrivateIPv4
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "$address=$null; if ($env:CHECK_VALUE -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' -and [Net.IPAddress]::TryParse($env:CHECK_VALUE, [ref]$address) -and $address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:addressBelongsToLocalHost
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "$addresses=[Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object { $_.GetIPProperties().UnicastAddresses } | ForEach-Object { $_.Address.IPAddressToString }; if ($addresses -contains $env:CHECK_VALUE) { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:portInUse
set "CHECK_VALUE=!%~1!"
powershell -NoProfile -Command "$ports=[Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port; if ($ports -contains [int]$env:CHECK_VALUE) { exit 0 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:portOwnedByProject
set "CHECK_VALUE=!%~1!"
docker ps --filter "label=com.docker.compose.project=%PROJECT_NAME%" --filter "label=com.docker.compose.service=inverterscout" --format "{{.Ports}}" 2>nul | findstr /r /c:":!CHECK_VALUE!->8080/tcp" >nul
exit /b %ERRORLEVEL%

:findFreePort
set /a "CANDIDATE=!%~1!+1"
set /a "PORT_LIMIT=CANDIDATE+99"
if !PORT_LIMIT! GTR 65535 set "PORT_LIMIT=65535"
set "%~2="
:findFreePortLoop
if !CANDIDATE! GTR !PORT_LIMIT! exit /b 1
call :portInUse CANDIDATE
if errorlevel 1 (
  set "%~2=!CANDIDATE!"
  exit /b 0
)
set /a "CANDIDATE+=1"
goto :findFreePortLoop

:askYesNo
set "QUESTION=%~1"
set "DEFAULT_ANSWER=%~2"
:askYesNoAgain
set "YES_NO="
if /I "%DEFAULT_ANSWER%"=="Y" (
  set /p "YES_NO=%QUESTION% [Y/n]: "
  if not defined YES_NO set "YES_NO=Y"
) else (
  set /p "YES_NO=%QUESTION% [y/N]: "
  if not defined YES_NO set "YES_NO=N"
)
if /I "!YES_NO!"=="Y" exit /b 0
if /I "!YES_NO!"=="YES" exit /b 0
if /I "!YES_NO!"=="N" exit /b 1
if /I "!YES_NO!"=="NO" exit /b 1
echo Please enter y or n.
goto :askYesNoAgain

:cleanupRemoteFiles
if defined BUNDLE_PATH if exist "%BUNDLE_PATH%" del /q "%BUNDLE_PATH%" >nul 2>&1
if defined OUTPUT_PATH if exist "%OUTPUT_PATH%" del /q "%OUTPUT_PATH%" >nul 2>&1
exit /b 0

:selfTest
set "TEST_VALUE=8080"
call :validatePort TEST_VALUE || (
  echo Windows launcher self-test failed: valid port rejected.
  exit /b 1
)
set "TEST_VALUE=70000"
call :validatePort TEST_VALUE
if not errorlevel 1 (
  echo Windows launcher self-test failed: invalid port accepted.
  exit /b 1
)
set "TEST_VALUE=inverter.local"
call :validateHost TEST_VALUE || (
  echo Windows launcher self-test failed: valid hostname rejected.
  exit /b 1
)
set "TEST_VALUE=bad/host"
call :validateHost TEST_VALUE
if not errorlevel 1 (
  echo Windows launcher self-test failed: invalid hostname accepted.
  exit /b 1
)
set "TEST_VALUE=scout_user"
call :validateUsername TEST_VALUE || (
  echo Windows launcher self-test failed: valid username rejected.
  exit /b 1
)
set "TEST_VALUE=192.0.2.10"
call :validateIPv4 TEST_VALUE || (
  echo Windows launcher self-test failed: valid IPv4 address rejected.
  exit /b 1
)
set "TEST_VALUE=192.168.1.20"
call :validatePrivateIPv4 TEST_VALUE || (
  echo Windows launcher self-test failed: private IPv4 address rejected.
  exit /b 1
)
set "TEST_VALUE=203.0.113.20"
call :validatePrivateIPv4 TEST_VALUE
if not errorlevel 1 (
  echo Windows launcher self-test failed: public IPv4 address accepted for LAN mode.
  exit /b 1
)
echo Windows launcher self-test passed.
exit /b 0

:help
echo InverterScout Quick Start
echo.
echo Usage: start.bat
echo.
echo The interactive launcher can deploy InverterScout locally with Docker
echo Desktop, to a home NAS or Docker-capable Arduino Linux board, or to a
echo remote Linux Docker host over SSH. It checks Docker
echo Compose, selects a free Web UI port, optionally verifies inverter TCP
echo reachability from the container, and starts the first-run setup wizard.
exit /b 0

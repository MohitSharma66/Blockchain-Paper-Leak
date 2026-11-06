@echo off
setlocal EnableExtensions

rem Starts ONE online-exam center in its own container: it creates the device identity (kept in a Docker
rem volume) and runs the exam server. The candidate list is NOT made here: the board creates it (board page,
rem "Candidate lists") and the exam server downloads it and checks it against the blockchain.
rem Re-running for the same center is refused.
rem The exam length is set by the board at generation. [exam-minutes] only matters for an old center that has none.
rem Usage: scripts\new-exam-center.cmd center_013 [exam-minutes]

if "%~1"=="" (
    echo Usage: scripts\new-exam-center.cmd center_013 [exam-minutes]
    exit /b 2
)

set "CENTER=%~1"
echo(%CENTER%| findstr /r /x "center_[0-9][0-9][0-9]" >nul
if errorlevel 1 (
    echo Invalid center ID. Use center_013 format.
    exit /b 2
)

set "DURATION_ARG="
if not "%~2"=="" set "DURATION_ARG=--duration-minutes %~2"

set "NUM=%CENTER:~-3%"
rem Port = 8080 + center number (center_001 -> 8081, center_013 -> 8093). The 1xxx trick avoids octal parsing of 013.
set /a PORT=8080+1%NUM%-1000

set "CONTAINER=neet-exam-%CENTER%"
rem Same identity volume name as new-printer.cmd: a center that prints AND runs online is one device.
set "IDVOL=minor7thsem_printer_%CENTER%"
set "DATAVOL=minor7thsem_exam_data_%CENTER%"

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is unavailable.
    exit /b 1
)

docker container inspect "%CONTAINER%" >nul 2>&1
if not errorlevel 1 (
    echo Container %CONTAINER% already exists. Not replacing it. ^(docker start %CONTAINER% to resume it^)
    exit /b 1
)

if not exist "backend\contract_deployment.json" (
    echo backend\contract_deployment.json is missing. Deploy the contract first.
    exit /b 1
)

rem Use the private-chain override files when they exist.
set "RPC_URL=http://node-board:8545"
set "COMPOSE_PATH_SEPARATOR=;"
set "COMPOSE_FILE=compose.yaml;chain\compose.exam.yaml"
if exist "chain\network\docker-compose.apps.yml" (
    set "COMPOSE_FILE=compose.yaml;chain\network\docker-compose.chain.yml;chain\network\docker-compose.apps.yml;chain\compose.exam.yaml"
    docker container inspect "neet-node-center-%NUM%" >nul 2>&1
    if errorlevel 1 (
        echo No chain node neet-node-center-%NUM% exists; using the board node.
    ) else (
        set "RPC_URL=http://node-center-%NUM%:8545"
    )
)

docker compose config --quiet
if errorlevel 1 exit /b 1

docker compose build exam-center-generic
if errorlevel 1 exit /b 1

docker volume inspect "%IDVOL%" >nul 2>&1
if errorlevel 1 (
    docker volume create "%IDVOL%" >nul
    if errorlevel 1 exit /b 1
)
docker volume inspect "%DATAVOL%" >nul 2>&1
if errorlevel 1 (
    docker volume create "%DATAVOL%" >nul
    if errorlevel 1 exit /b 1
)

echo Starting %CENTER% on chain node %RPC_URL% ...

docker compose run -d --no-deps --name "%CONTAINER%" ^
    -p 127.0.0.1:%PORT%:8080 ^
    -e CHAIN_RPC_URL=%RPC_URL% ^
    --volume "%IDVOL%:/app/printer-agent/devices/%CENTER%" ^
    --volume "%DATAVOL%:/data" ^
    exam-center-generic run --center "%CENTER%" --home /app/printer-agent/devices --rpc-url "%RPC_URL%" --api-url http://backend:8000 --port 8080 %DURATION_ARG% --data-dir /data

if errorlevel 1 (
    echo Failed to create the exam container.
    exit /b 1
)

docker update --restart unless-stopped "%CONTAINER%" >nul
if errorlevel 1 (
    echo WARNING: Could not set the automatic restart policy.
)

echo.
echo Exam container : %CONTAINER%
echo Candidate page : http://localhost:%PORT%/
echo Identity volume: %IDVOL%
echo.
echo Next:
echo   1. Upload printer-agent\enrollments\enrollment_%CENTER%.json on the board page ("Enroll printers...").
echo   2. Create the candidate list on the board page ^("Candidate lists"^) and download the admit-card codes.
echo   3. Register the center on the board page. Watch it at http://localhost:8000/ui/online.html?center=%CENTER%
echo   Logs: docker logs %CONTAINER%

endlocal

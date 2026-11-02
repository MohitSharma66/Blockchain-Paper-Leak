
@echo off
setlocal EnableExtensions

if "%~1"=="" (
    echo Usage: scripts\new-printer.cmd center_006
    exit /b 2
)

set "CENTER=%~1"
echo(%CENTER%| findstr /r /x "center_[0-9][0-9][0-9]" >nul
if errorlevel 1 (
    echo Invalid center ID. Use center_006 format.
    exit /b 2
)

set "NUM=%CENTER:~-3%"
set "CONTAINER=neet-printer-%CENTER%"
set "VOLUME=minor7thsem_printer_%CENTER%"

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is unavailable.
    exit /b 1
)

docker container inspect "%CONTAINER%" >nul 2>&1
if not errorlevel 1 (
    echo Container already exists. Not replacing it.
    exit /b 1
)

rem Private chain: use the chain overrides if they were generated (python chain\make_network.py).
set "RPC_ENV="
if exist "chain\network\docker-compose.apps.yml" (
    set "COMPOSE_FILE=compose.yaml;chain\network\docker-compose.chain.yml;chain\network\docker-compose.apps.yml"
    set "COMPOSE_PATH_SEPARATOR=;"
    docker container inspect "neet-node-center-%NUM%" >nul 2>&1
    if errorlevel 1 (
        echo No chain node neet-node-center-%NUM% exists; using the board node. Make a node for it with make_network.py --centers N.
        set "RPC_ENV=-e CHAIN_RPC_URL=http://node-board:8545 -e RPC_URL=http://node-board:8545"
    ) else (
        set "RPC_ENV=-e CHAIN_RPC_URL=http://node-center-%NUM%:8545 -e RPC_URL=http://node-center-%NUM%:8545"
    )
)

docker compose config --quiet
if errorlevel 1 exit /b 1

docker compose build printer-center-generic
if errorlevel 1 exit /b 1

docker volume inspect "%VOLUME%" >nul 2>&1
if errorlevel 1 (
    docker volume create "%VOLUME%" >nul
    if errorlevel 1 exit /b 1
)

echo Starting %CENTER% with its dedicated volume...

docker compose run -d --no-deps %RPC_ENV% --name "%CONTAINER%" ^
    --volume "%VOLUME%:/app/printer-agent/devices/%CENTER%" ^
    printer-center-generic run --center "%CENTER%" --home /app/printer-agent/devices --api-url http://backend:8000

if errorlevel 1 (
    echo Failed to create printer container.
    exit /b 1
)

docker update --restart unless-stopped "%CONTAINER%" >nul
if errorlevel 1 (
    echo WARNING: Could not set the automatic restart policy.
)

echo.
echo Printer container: %CONTAINER%
echo Dedicated volume: %VOLUME%
echo Check logs: docker logs %CONTAINER%

endlocal

#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  start_patched.sh — Version corrigée de /Metatrader/start.sh
# ─────────────────────────────────────────────────────────────

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
mt5dir='/config/.wine/drive_c/Program Files/MetaTrader 5'
mt5config_dir='/config/.wine/drive_c/Program Files/MetaTrader 5/Config'
mt5exe_config='/config/.wine/drive_c/Program Files/MetaTrader 5/config.ini'
WINEPREFIX='/config/.wine'
WINEDEBUG='-all'
export WINEPREFIX WINEDEBUG
metatrader_version="5.0.36"
mt5server_port="8001"
mono_url="https://dl.winehq.org/wine/wine-mono/10.3.0/wine-mono-10.3.0-x86.msi"
python_url="https://www.python.org/ftp/python/3.9.13/python-3.9.13.exe"
mt5setup_url="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
WINE_PYTHON="C:/Program Files (x86)/Python39-32/python.exe"
RPYC_SERVER="/fix/rpyc_server.py"



show_message() { echo "$1"; }

check_dependency() {
    if ! command -v $1 &> /dev/null; then echo "$1 is not installed."; exit 1; fi
}

is_python_package_installed() {
    python3 -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
}

is_wine_python_package_installed() {
    wine python -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
}

check_dependency "curl"
check_dependency "wine"

# [1/7] Mono
if [ ! -e "/config/.wine/drive_c/windows/mono" ]; then
    show_message "[1/7] Downloading and installing Mono..."
    curl -o /tmp/mono.msi $mono_url
    WINEDLLOVERRIDES=mscoree=d wine msiexec /i /tmp/mono.msi /qn
    rm -f /tmp/mono.msi
    show_message "[1/7] Mono installed."
else
    show_message "[1/7] Mono is already installed."
fi

# [2/7] MT5
if [ -e "$mt5file" ]; then
    show_message "[2/7] File $mt5file already exists."
else
    show_message "[2/7] File $mt5file is not installed. Installing..."
    wine reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    show_message "[3/7] Downloading MT5 installer..."
    curl -o /tmp/mt5setup.exe $mt5setup_url
    show_message "[3/7] Installing MetaTrader 5..."
    wine /tmp/mt5setup.exe /auto &
    wait
    rm -f /tmp/mt5setup.exe

    # forbid liveUpdating.
    rm -rf /config/.wine/drive_c/users/abc/AppData/Roaming/MetaQuotes/WebInstall || true
    mkdir /config/.wine/drive_c/users/abc/AppData/Roaming/MetaQuotes/WebInstall
    chmod 555 /config/.wine/drive_c/users/abc/AppData/Roaming/MetaQuotes/WebInstall
fi

# [3.5/7] installation EA DataBridge
EA_DIR="$mt5dir/MQL5/Experts"
mkdir -p "$EA_DIR"
EA_MQ5="$EA_DIR/DataBridge.mq5"
EA_EX5="$EA_DIR/DataBridge.ex5"
cp /fix/DataBridge.mq5 "$EA_MQ5"
cp /fix/DataBridge.ex5 "$EA_EX5"
ls "$EA_DIR"
show_message "[3.5/7] installation de DataBridge.mq5 copié ✓"


# [4/7] Copie des fichiers de credentials sauvegardés + lancement MT5
if [ -e "$mt5file" ]; then
    show_message "[4/7] File $mt5file is installed."
    mkdir -p "$mt5config_dir"

    if [ -f "/fix/servers.dat" ]; then
        cp /fix/servers.dat "$mt5config_dir/servers.dat"
        show_message "[4/7] servers.dat restauré ✓"
    fi

    # ── Génération dynamique du config.ini ────────────────────
    # Priorité 1 : variables d'env MT5_LOGIN + MT5_PASSWORD + MT5_SERVER
    # Priorité 2 : fichier /fix/config.ini statique monté en volume
    # Priorité 3 : MT5 démarre sans config (écran de login manuel)
    if [ -n "${MT5_LOGIN}" ] && [ -n "${MT5_PASSWORD}" ] && [ -n "${MT5_SERVER}" ]; then
        show_message "[4/7] Génération config.ini depuis variables d'environnement..."
        printf '[Common]\nLogin=%s\nPassword=%s\nServer=%s\nProxyEnable=0\nCertConfirm=1\nNewsEnable=1\nLiveUpdate=0\n[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\n[StartUp]\nProfile=default\nSymbol=EURUSD\nPeriod=H1\nExpert=DataBridge\n' \
            "${MT5_LOGIN}" "${MT5_PASSWORD}" "${MT5_SERVER}" > "$mt5exe_config"
        show_message "[4/7] config.ini généré avec credentials broker ✓"
        wine "$mt5file" /config:"C:\\Program Files\\MetaTrader 5\\config.ini" &


    elif [ -f "/fix/config.ini" ]; then
        show_message "[4/7] Utilisation du config.ini statique..."
        cp /fix/config.ini "$mt5exe_config"
        wine "$mt5file" /config:"C:\\Program Files\\MetaTrader 5\\config.ini" &

    else
        show_message "[4/7] Aucun config — MT5 démarre en mode manuel."
        printf '[Common]\nProxyEnable=0\nCertConfirm=1\nNewsEnable=1\n[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\n[StartUp]\nProfile=default\nSymbol=EURUSD\nPeriod=H1\nExpert=DataBridge\n' \
            > "$mt5exe_config"
        wine "$mt5file" /config:"C:\\Program Files\\MetaTrader 5\\config.ini" &
    fi
else
    show_message "[4/7] File $mt5file is not installed. MT5 cannot be run."
fi

# [5/7] Python
if ! wine python --version 2>/dev/null; then
    show_message "[5/7] Installing Python in Wine..."
    curl -L $python_url -o /tmp/python-installer.exe
    wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    rm /tmp/python-installer.exe
    show_message "[5/7] Python installed in Wine."
else
    show_message "[5/7] Python is already installed in Wine."
fi

# [6/7] Librairies Python Windows
show_message "[6/7] Installing Python libraries"
wine python -m pip install --upgrade --no-cache-dir pip

show_message "[6/7] Installing numpy<2 + MetaTrader5 in Windows"
wine python -m pip install --no-cache-dir "numpy<2" tzdata pytz
if ! is_wine_python_package_installed "MetaTrader5==$metatrader_version"; then
    wine python -m pip install --no-cache-dir MetaTrader5==$metatrader_version
fi

show_message "[6/7] Checking and installing mt5linux in Windows"
if ! is_wine_python_package_installed "mt5linux"; then
    wine python -m pip install --no-cache-dir "mt5linux>=0.1.9"
fi

if ! is_wine_python_package_installed "python-dateutil"; then
    show_message "[6/7] Installing python-dateutil in Windows"
    wine python -m pip install --no-cache-dir python-dateutil
fi

# [6/7] Librairies Python Linux
show_message "[6/7] Checking and installing mt5linux in Linux"
if ! is_python_package_installed "mt5linux"; then
    pip install --break-system-packages --no-cache-dir --no-deps mt5linux && \
    pip install --break-system-packages --no-cache-dir rpyc==5.2.3 plumbum==1.7.0 "pyparsing>=3.1.0" numpy
fi

show_message "[6/7] Checking and installing pyxdg in Linux"
if ! is_python_package_installed "pyxdg"; then
    pip install --break-system-packages --no-cache-dir pyxdg
fi

# [7/7] Serveur RPyC avec Python Windows
show_message "[7/7] Starting the mt5linux RPyC server..."
wine "$WINE_PYTHON" "$RPYC_SERVER" &

sleep 5

if ss -tuln | grep ":$mt5server_port" > /dev/null; then
    show_message "[7/7] The mt5linux server is running on port $mt5server_port."
else
    show_message "[7/7] Failed to start the mt5linux server on port $mt5server_port."
fi
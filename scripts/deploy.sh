#!/usr/bin/env bash
# ==============================================================================
# SPONTIFY / SELF-HOSTED MUSIC STACK - AUTOMATED DEPLOYMENT SCRIPT FOR DEBIAN
# ==============================================================================

set -euo pipefail

# Kolory do komunikatów
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

INSTALL_DIR="/opt/music-stack"

echo -e "${BLUE}==============================================================================${NC}"
echo -e "${BLUE}   Automatyczny instalator prywatnego ekosystemu muzycznego (Spotify Stack)  ${NC}"
echo -e "${BLUE}==============================================================================${NC}"

# 1. Weryfikacja uprawnień roota
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}[!] Ten skrypt musi zostać uruchomiony z uprawnieniami roota (sudo).${NC}"
   exit 1
fi

# 2. Aktualizacja pakietów i instalacja podstawowych narzędzi
echo -e "\n${YELLOW}[1/6] Aktualizacja systemu Debian i instalacja zależności...${NC}"
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    git \
    ffmpeg

# 3. Weryfikacja / Instalacja Dockera i Docker Compose v2
echo -e "\n${YELLOW}[2/6] Weryfikacja środowiska Docker...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${BLUE}[*] Instalacja oficjalnego pakietu Docker CE...${NC}"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo -e "${GREEN}[+] Docker zainstalowany pomyślnie.${NC}"
else
    echo -e "${GREEN}[+] Docker jest już zainstalowany.${NC}"
fi

# 4. Przygotowanie struktury katalogów /opt/music-stack
echo -e "\n${YELLOW}[3/6] Przygotowanie struktury katalogów w ${INSTALL_DIR}...${NC}"
mkdir -p "${INSTALL_DIR}/music"
mkdir -p "${INSTALL_DIR}/data/navidrome"
mkdir -p "${INSTALL_DIR}/data/downloader/temp"
mkdir -p "${INSTALL_DIR}/data/slskd"

# Jeśli uruchamiamy z poziomu sklonowanego repozytorium, kopiujemy pliki do /opt/music-stack
CURRENT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$CURRENT_SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    echo -e "${BLUE}[*] Kopiowanie plików aplikacji do ${INSTALL_DIR}...${NC}"
    cp -ru "${CURRENT_SCRIPT_DIR}/." "${INSTALL_DIR}/"
fi

# 5. Konfiguracja uprawnień PUID/PGID 1000:1000
echo -e "\n${YELLOW}[4/6] Ustawianie uprawnień dla wolumenów dyskowych (1000:1000)...${NC}"
chown -R 1000:1000 "${INSTALL_DIR}/music"
chown -R 1000:1000 "${INSTALL_DIR}/data"
chmod -R 775 "${INSTALL_DIR}/music"
chmod -R 775 "${INSTALL_DIR}/data"

# 6. Sprawdzenie pliku .env
echo -e "\n${YELLOW}[5/6] Weryfikacja konfiguracji .env...${NC}"
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    if [[ -f "${INSTALL_DIR}/.env.example" ]]; then
        echo -e "${YELLOW}[!] Tworzenie pliku .env z szablonu .env.example...${NC}"
        cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
        echo -e "${RED}[!] PAMIĘTAJ: Uzupełnij SPOTIFY_CLIENT_ID oraz SPOTIFY_CLIENT_SECRET w ${INSTALL_DIR}/.env!${NC}"
    fi
fi

# 7. Budowanie i start kontenerów Docker Compose
echo -e "\n${YELLOW}[6/6] Budowanie i uruchamianie kontenerów w tle...${NC}"
cd "${INSTALL_DIR}"
docker compose down || true
docker compose up -d --build

echo -e "\n${GREEN}==============================================================================${NC}"
echo -e "${GREEN}   Wdrożenie zakończone sukcesem!                                             ${NC}"
echo -e "${GREEN}==============================================================================${NC}"
echo -e "Serwisy działają pod adresami:"
echo -e "  - Feishin Web (Spotify 1:1): ${BLUE}http://$(hostname -I | awk '{print $1}'):9180${NC}"
echo -e "  - Navidrome Server:         ${BLUE}http://$(hostname -I | awk '{print $1}'):4533${NC}"
echo -e "  - Downloader Dashboard:     ${BLUE}http://$(hostname -I | awk '{print $1}'):8000${NC}"
echo -e "  - Dokumentacja API Swagger: ${BLUE}http://$(hostname -I | awk '{print $1}'):8000/docs${NC}"
echo -e "\nKolejne kroki:"
echo -e "  1. Wejdź na http://IP_SERWERA:4533 i utwórz konto administratora Navidrome."
echo -e "  2. Wpisz to samo hasło w pliku ${INSTALL_DIR}/.env (NAVIDROME_ADMIN_PASSWORD), aby odświeżanie działało natychmiast."
echo -e "  3. Wpisz dane ze Spotify Developer Dashboard w .env i zrestartuj API: docker compose restart music-downloader-api."
echo -e "==============================================================================\n"

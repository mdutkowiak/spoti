#!/usr/bin/env bash
# ==============================================================================
# SPONTIFY / SELF-HOSTED MUSIC STACK - CLI TEST DOWNLOAD SCRIPT
# ==============================================================================

API_URL="${API_URL:-http://localhost:8000}"
QUERY="${1:-https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT}" # Domyślnie Rick Astley - Never Gonna Give You Up lub dowolny link
FORMAT="${2:-opus}"

echo "=================================================================="
echo " Testowanie Music Downloader API"
echo " Query:  ${QUERY}"
echo " Format: ${FORMAT}"
echo " API:    ${API_URL}"
echo "=================================================================="

# 1. Health check
echo -n "Sprawdzanie stanu API... "
HEALTH=$(curl -s "${API_URL}/health")
echo "${HEALTH}"

# 2. Zlecenie zadania
echo -n "Zlecanie zadania pobierania... "
RESP=$(curl -s -X POST "${API_URL}/download" \
  -H "Content-Type: application/json" \
  -d "{\"query_or_url\": \"${QUERY}\", \"format\": \"${FORMAT}\"}")

TASK_ID=$(echo "$RESP" | grep -o '"task_id":"[^"]*' | cut -d'"' -f4)

if [[ -z "$TASK_ID" ]]; then
    echo "BŁĄD: Nie otrzymano task_id. Odpowiedź serwera: $RESP"
    exit 1
fi

echo "Pomyślnie utworzono zadanie o ID: ${TASK_ID}"

# 3. Polling statusu
echo "Oczekiwanie na zakończenie pobierania..."
while true; do
    STATUS_JSON=$(curl -s "${API_URL}/tasks/${TASK_ID}")
    STATUS=$(echo "$STATUS_JSON" | grep -o '"status":"[^"]*' | cut -d'"' -f4)
    TRACK=$(echo "$STATUS_JSON" | grep -o '"current_track":"[^"]*' | cut -d'"' -f4)

    echo "  -> Stan: [${STATUS}] Aktualnie: ${TRACK}"

    if [[ "$STATUS" == "success" ]]; then
        echo "=================================================================="
        echo " SUKCES! Utwór pobrany i otagowany. Navidrome został zaktualizowany."
        echo "=================================================================="
        break
    elif [[ "$STATUS" == "failed" ]]; then
        echo "=================================================================="
        echo " BŁĄD! Zadanie zakończyło się niepowodzeniem:"
        echo "$STATUS_JSON"
        echo "=================================================================="
        exit 1
    fi

    sleep 2
done

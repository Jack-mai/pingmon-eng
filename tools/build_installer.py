#!/usr/bin/env python3
"""Tao file ./installer (1 file duy nhat, chua san toan bo ma nguon) tu docker-compose.yml va app/.
Chay lai moi khi sua app/app.py, app/index.html hoac docker-compose.yml:
    python3 tools/build_installer.py
"""
import os
SRC=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT=os.path.join(SRC,"installer")
files=["docker-compose.yml","app/app.py","app/index.html"]
head=r'''#!/usr/bin/env bash
# =====================================================================
#  INSTALLER - Website giam sat mang noi bo (pingmon)
#  Ping / TCP / HTTP / DNS - phat am thanh khi thiet bi doi trang thai
#  May chu: awx (10.10.90.183) - https://monitor.cpt.gov.vn
#
#  Cach dung:
#     chmod +x installer && ./installer     # cai dat / cap nhat
#     ./installer status | logs | password | restart | uninstall
#
#  Bien tuy chon: TARGET=~/pingmon DOMAIN=... IP=... HTTP_PORT=80 HTTPS_PORT=443
#                 AUTO_STOP=1  (tu dung container dang chiem cong, khong hoi)
# =====================================================================
set -euo pipefail

TARGET="${TARGET:-$HOME/pingmon}"
DOMAIN="${DOMAIN:-monitor.cpt.gov.vn}"
IP="${IP:-10.10.90.183}"
HOST_NAME="${HOST_NAME:-awx}"
OLD_CHECKMATE="${OLD_CHECKMATE:-$HOME/check}"

if [[ -f "$TARGET/.env" ]]; then
  _h=$(grep -s '^HTTP_PORT=' "$TARGET/.env" | cut -d= -f2 || true)
  _s=$(grep -s '^HTTPS_PORT=' "$TARGET/.env" | cut -d= -f2 || true)
fi
HTTP_PORT="${HTTP_PORT:-${_h:-80}}"
HTTPS_PORT="${HTTPS_PORT:-${_s:-443}}"

G='\033[1;32m'; Y='\033[1;33m'; R='\033[1;31m'; B='\033[1;36m'; N='\033[0m'
step() { echo -e "\n${B}==> $*${N}"; }
ok()   { echo -e "${G}  ✔ $*${N}"; }
warn() { echo -e "${Y}  ! $*${N}"; }
die()  { echo -e "${R}  ✘ $*${N}"; exit 1; }

SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
DOCKER="docker"
pick_docker() { if docker info >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="$SUDO docker"; fi; }
ask() { # ask "cau hoi" -> 0 neu dong y
  [[ "${AUTO_STOP:-0}" == "1" ]] && return 0
  [[ -t 0 ]] || return 1
  local a; read -r -p "  ? $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[YyCc] ]]
}

case "${1:-install}" in
  status)    pick_docker; cd "$TARGET" && $DOCKER compose ps; exit 0 ;;
  logs)      pick_docker; cd "$TARGET" && exec $DOCKER compose logs -f --tail 100 ;;
  restart)   pick_docker; cd "$TARGET" && $DOCKER compose restart && ok "Da khoi dong lai"; exit 0 ;;
  password)  grep '^ADMIN_PASSWORD=' "$TARGET/.env" | cut -d= -f2; exit 0 ;;
  uninstall) pick_docker; cd "$TARGET" && $DOCKER compose down
             ok "Da go container. Du lieu van con trong $TARGET/data"; exit 0 ;;
  install) ;;
  *) echo "Dung: $0 [install|status|logs|password|restart|uninstall]"; exit 1 ;;
esac

# ---------------------------------------------------------------- 1. He thong
step "Kiem tra he thong"
[[ -f /etc/os-release ]] && . /etc/os-release && ok "He dieu hanh: ${PRETTY_NAME:-?}"
need_pkg() { command -v "$1" >/dev/null || { $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$2"; }; }
need_pkg openssl openssl; need_pkg curl curl

# ---------------------------------------------------------------- 2. Docker
step "Docker"
if ! command -v docker >/dev/null; then
  warn "Chua co Docker -> dang cai dat..."
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq docker.io docker-compose-v2 || curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
fi
pick_docker
$DOCKER compose version >/dev/null 2>&1 || $SUDO apt-get install -y -qq docker-compose-v2 || $SUDO apt-get install -y -qq docker-compose-plugin
ok "$($DOCKER --version)"
ok "$($DOCKER compose version)"

# ---------------------------------------------------------------- 3. Cong
step "Kiem tra cong ${HTTP_PORT}/${HTTPS_PORT}"
owners_of_port() { # ten cac container KHAC (moi trang thai) dang khai bao cong host $1
  local c n ports
  for c in $($DOCKER ps -aq); do
    n=$($DOCKER inspect -f '{{.Name}}' "$c" | sed 's#^/##')
    [[ "$n" == "pingmon" ]] && continue
    ports=$($DOCKER inspect -f '{{range $p,$b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}} {{end}}{{end}}' "$c")
    tr ' ' '\n' <<<"$ports" | grep -qx "$1" && echo "$n"
  done
  return 0
}
is_running() { [[ "$($DOCKER inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }

for p in "$HTTP_PORT" "$HTTPS_PORT"; do
  for n in $(owners_of_port "$p"); do
    if is_running "$n"; then
      if [[ "$n" == checkmate* && -f "$OLD_CHECKMATE/docker-compose.yml" ]]; then
        warn "Cong $p dang duoc he thong Checkmate cu dung ($n)"
        if ask "Dung he thong Checkmate cu (du lieu van giu trong $OLD_CHECKMATE/data)?"; then
          (cd "$OLD_CHECKMATE" && $DOCKER compose down) && ok "Da dung Checkmate"
          continue
        fi
      else
        warn "Cong $p dang bi container '$n' chiem ($($DOCKER inspect -f '{{.Config.Image}}' "$n"))"
        if ask "Dung container '$n' de nhuong cong cho website giam sat?"; then
          $DOCKER stop "$n" >/dev/null && $DOCKER update --restart=no "$n" >/dev/null && ok "Da dung $n (khong tu bat lai khi khoi dong may)"
          continue
        fi
      fi
      echo -e "
${Y}  Cach xu ly:${N}
   a) Chay lai va tra loi Y, hoac:  AUTO_STOP=1 $0
   b) Dung cong khac:               HTTP_PORT=8080 HTTPS_PORT=8443 $0"
      die "Cong $p dang bi chiem - chua cai dat"
    else
      # container da dung nhung van khai bao cong nay -> chan khong cho no tu bat lai luc khoi dong may
      if [[ "$($DOCKER inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$n")" =~ ^(always|unless-stopped)$ ]]; then
        $DOCKER update --restart=no "$n" >/dev/null && warn "Container '$n' (da dung) cung dung cong $p -> tat tu khoi dong de tranh tranh chap"
      fi
    fi
  done
  if command -v ss >/dev/null && ss -ltnH "sport = :$p" 2>/dev/null | grep -q . \
     && ! $DOCKER ps --filter name='^pingmon$' -q | grep -q .; then
    if ! ss -ltnpH "sport = :$p" 2>/dev/null | grep -q docker-proxy; then
      warn "Cong $p dang bi chuong trinh tren may chiem:"; ss -ltnpH "sport = :$p" | sed 's/^/      /'
      die "Tat chuong trinh do (vd: systemctl disable --now apache2 nginx) hoac dung HTTPS_PORT khac"
    fi
  fi
  ok "Cong $p san sang"
done

# ---------------------------------------------------------------- 4. Ghi file
step "Ghi file vao $TARGET"
mkdir -p "$TARGET"/{app,data,ssl}
cd "$TARGET"
'''
body=""
for i,f in enumerate(files):
    c=open(os.path.join(SRC,f),encoding="utf-8").read()
    tag=f"__PINGMON_EOF_{i}__"; assert tag not in c
    body+=f"cat > '{f}' <<'{tag}'\n{c}{'' if c.endswith(chr(10)) else chr(10)}{tag}\nok '{f}'\n\n"
tail=r'''# ---------------------------------------------------------------- 5. .env
step "Cau hinh"
if [[ ! -f .env ]]; then
  echo "ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-12)" > .env
  chmod 600 .env
  ok "Da tao mat khau quan tri ngau nhien"
else
  ok "Giu nguyen mat khau quan tri hien co"
fi
setenv() { if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi; }
setenv HTTP_PORT "$HTTP_PORT"; setenv HTTPS_PORT "$HTTPS_PORT"
if [[ "$HTTPS_PORT" == "443" ]]; then URL="https://${DOMAIN}"; else URL="https://${DOMAIN}:${HTTPS_PORT}"; fi
ok "Dia chi: $URL"

# ---------------------------------------------------------------- 6. Chung chi
step "Chung chi SSL"
if [[ -f ssl/pingmon.crt && -f ssl/pingmon.key ]]; then
  ok "Giu nguyen chung chi hien co"
elif [[ -f "$OLD_CHECKMATE/nginx/ssl/checkmate.crt" && -f "$OLD_CHECKMATE/nginx/ssl/checkmate.key" ]]; then
  $SUDO cp "$OLD_CHECKMATE/nginx/ssl/checkmate.crt" ssl/pingmon.crt
  $SUDO cp "$OLD_CHECKMATE/nginx/ssl/checkmate.key" ssl/pingmon.key
  $SUDO chown "$(id -u):$(id -g)" ssl/pingmon.*
  ok "Dung lai chung chi cua Checkmate -> may tram da cai checkmate.crt khong can cai lai"
else
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
    -keyout ssl/pingmon.key -out ssl/pingmon.crt -subj "/C=VN/O=CPT/CN=${DOMAIN}" \
    -addext "subjectAltName=DNS:${DOMAIN},DNS:${HOST_NAME},IP:${IP}" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
  ok "Da tao chung chi tu ky 10 nam (SAN: ${DOMAIN}, ${HOST_NAME}, ${IP})"
fi
chmod 600 ssl/pingmon.key; chmod 644 ssl/pingmon.crt; chmod 700 ssl
cp ssl/pingmon.crt ./pingmon.crt
openssl x509 -in ssl/pingmon.crt -noout -ext subjectAltName 2>/dev/null | tail -1 | sed 's/^ */  /'

# ---------------------------------------------------------------- 7. Tuong lua
if command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  $SUDO ufw allow "${HTTP_PORT}/tcp" >/dev/null && $SUDO ufw allow "${HTTPS_PORT}/tcp" >/dev/null && ok "UFW: da mo cong ${HTTP_PORT}/${HTTPS_PORT}"
fi

# ---------------------------------------------------------------- 8. Khoi dong
step "Khoi dong"
$DOCKER compose pull -q
$DOCKER compose up -d --remove-orphans --force-recreate

step "Cho website san sang"
READY=0
for i in $(seq 1 40); do
  if curl -ks --max-time 3 "https://127.0.0.1:${HTTPS_PORT}/healthz" | grep -q '"ok"'; then READY=1; break; fi
  sleep 2
done
if [[ $READY == 1 ]]; then ok "Website da chay"; else
  warn "Website chua phan hoi. Nhat ky:"; $DOCKER compose logs --tail 30; die "Xem loi o tren"; fi
$DOCKER compose ps

# Thu ping tu trong container
if $DOCKER exec pingmon ping -c 1 -W 2 "$IP" >/dev/null 2>&1; then ok "Container ping duoc ${IP}"
else warn "Container chua ping duoc ${IP} - kiem tra lai neu thiet bi luon bao mat ket noi"; fi

PW=$(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2)
echo -e "
${G}=====================================================================${N}
${G} CAI DAT XONG${N}

 Website        : ${URL}
 Mat khau QT    : ${Y}${PW}${N}   (xem lai: ./installer password)
 Thu muc        : ${TARGET}

 BUOC TIEP THEO:
 1. Mo ${URL} -> 'Dang nhap quan tri' -> '+ Them thiet bi'
    hoac 'Nhap danh sach' (dan tu Excel: Ten, IP)
 2. May truc: mo ${URL}, bam 'Bat am thanh thong bao'.
    Tu dong khi bat may (khong can bam): shortcut trong shell:startup
      chrome.exe --kiosk --autoplay-policy=no-user-gesture-required ${URL}
 3. May tram chua tin chung chi: cai ${TARGET}/pingmon.crt
      certutil -addstore -f Root pingmon.crt   (CMD quyen Admin)

 Lenh: ./installer status | logs | password | restart | uninstall
${G}=====================================================================${N}
"
'''
open(OUT,"w",encoding="utf-8",newline="\n").write(head+body+tail)
os.chmod(OUT,0o755)
print("Da tao", OUT)

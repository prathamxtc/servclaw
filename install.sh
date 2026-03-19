#!/usr/bin/env bash
set -euo pipefail

# The app lives in the same directory as this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"
CONFIG_FILE="$APP_DIR/servclaw.json"
SERVCLAW_CLI_SOURCE="$APP_DIR/servclaw"
OS_NAME=""
OS_VERSION=""
ARCH_RAW=""
ARCH_NORM=""
PKG_MANAGER=""
DOCKER_CMD="docker"
COMPOSE_CMD=(docker compose)

log() {
  printf '[install] %s\n' "$*"
}

warn() {
  printf '[install][warn] %s\n' "$*" >&2
}

die() {
  printf '[install][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "required command '$1' not found in PATH"
  fi
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

detect_os_arch() {
  ARCH_RAW="$(uname -m 2>/dev/null || echo unknown)"
  case "$ARCH_RAW" in
    x86_64|amd64) ARCH_NORM="amd64" ;;
    aarch64|arm64) ARCH_NORM="arm64" ;;
    armv7l|armv6l) ARCH_NORM="armhf" ;;
    *) ARCH_NORM="$ARCH_RAW" ;;
  esac

  case "$(uname -s 2>/dev/null || echo unknown)" in
    Linux)
      OS_NAME="linux"
      if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_VERSION="${VERSION_ID:-unknown}"
        local os_id="${ID:-}"
        local os_like="${ID_LIKE:-}"
        if [[ "$os_id" == "ubuntu" || "$os_id" == "debian" || "$os_like" == *"debian"* ]]; then
          PKG_MANAGER="apt"
        elif [[ "$os_id" == "fedora" ]]; then
          PKG_MANAGER="dnf"
        elif [[ "$os_id" == "rhel" || "$os_id" == "centos" || "$os_id" == "rocky" || "$os_id" == "almalinux" || "$os_like" == *"rhel"* ]]; then
          if has_cmd dnf; then
            PKG_MANAGER="dnf"
          else
            PKG_MANAGER="yum"
          fi
        elif [[ "$os_id" == "arch" || "$os_like" == *"arch"* ]]; then
          PKG_MANAGER="pacman"
        elif [[ "$os_id" == "opensuse-tumbleweed" || "$os_id" == opensuse* || "$os_like" == *"suse"* ]]; then
          PKG_MANAGER="zypper"
        else
          PKG_MANAGER="unknown"
        fi
      else
        OS_VERSION="unknown"
        PKG_MANAGER="unknown"
      fi
      ;;
    Darwin)
      OS_NAME="macos"
      OS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
      PKG_MANAGER="brew"
      ;;
    *)
      OS_NAME="unknown"
      OS_VERSION="unknown"
      PKG_MANAGER="unknown"
      ;;
  esac
}

run_pkg_install() {
  local manager="$1"
  shift
  case "$manager" in
    apt)
      sudo apt-get update -y
      sudo apt-get install -y "$@"
      ;;
    dnf)
      sudo dnf install -y "$@"
      ;;
    yum)
      sudo yum install -y "$@"
      ;;
    pacman)
      sudo pacman -Sy --noconfirm "$@"
      ;;
    zypper)
      sudo zypper --non-interactive install "$@"
      ;;
    brew)
      brew install "$@"
      ;;
    *)
      return 1
      ;;
  esac
}

install_docker_and_compose_if_needed() {
  local need_docker=0
  local need_compose=0

  if ! has_cmd docker; then
    need_docker=1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    need_compose=1
  fi

  if [[ "$need_docker" -eq 0 && "$need_compose" -eq 0 ]]; then
    log "Docker and Docker Compose are already available."
    return
  fi

  if [[ "$OS_NAME" == "unknown" || "$PKG_MANAGER" == "unknown" ]]; then
    die "unsupported OS/package manager for automatic install. Install Docker + Compose manually."
  fi

  if [[ "$OS_NAME" == "macos" ]]; then
    if ! has_cmd brew; then
      die "Homebrew is required for automatic install on macOS. Install brew first: https://brew.sh"
    fi
    if [[ "$need_docker" -eq 1 || "$need_compose" -eq 1 ]]; then
      warn "Installing Docker Desktop cask via Homebrew. You may need to launch Docker.app once."
      brew install --cask docker || true
    fi
    return
  fi

  require_cmd sudo

  case "$PKG_MANAGER" in
    apt)
      log "Installing Docker/Compose via apt..."
      run_pkg_install apt ca-certificates curl gnupg lsb-release software-properties-common
      run_pkg_install apt docker.io docker-compose-plugin || run_pkg_install apt docker.io
      ;;
    dnf)
      log "Installing Docker/Compose via dnf..."
      run_pkg_install dnf dnf-plugins-core
      run_pkg_install dnf docker docker-compose-plugin || run_pkg_install dnf docker
      ;;
    yum)
      log "Installing Docker/Compose via yum..."
      run_pkg_install yum yum-utils
      run_pkg_install yum docker docker-compose-plugin || run_pkg_install yum docker
      ;;
    pacman)
      log "Installing Docker/Compose via pacman..."
      run_pkg_install pacman docker docker-compose
      ;;
    zypper)
      log "Installing Docker/Compose via zypper..."
      run_pkg_install zypper docker docker-compose || run_pkg_install zypper docker
      ;;
    *)
      die "unsupported package manager '$PKG_MANAGER'"
      ;;
  esac
}

ensure_docker_daemon() {
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
    COMPOSE_CMD=(docker compose)
    return
  fi

  warn "Docker daemon is not accessible for current user. Attempting to start/enable it."
  if has_cmd sudo && has_cmd systemctl; then
    sudo systemctl enable --now docker || true
  fi

  if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
    COMPOSE_CMD=(docker compose)
    return
  fi

  if has_cmd sudo && sudo docker info >/dev/null 2>&1; then
    warn "Using sudo docker for this run."
    DOCKER_CMD="sudo docker"
    COMPOSE_CMD=(sudo docker compose)
    return
  fi

  if has_cmd sudo && id -nG "$USER" | grep -qw docker; then
    warn "User is in docker group but session may be stale. Try: newgrp docker"
  elif has_cmd sudo; then
    warn "Adding current user to docker group for future sessions."
    sudo usermod -aG docker "$USER" || true
  fi

  die "Docker daemon still not accessible. Start Docker and re-run installer."
}

compose_up() {
  "${COMPOSE_CMD[@]}" up -d --build servclaw
}

compose_ps() {
  "${COMPOSE_CMD[@]}" ps
}

get_existing_json_value() {
  local key_path="$1"
  local file="$2"
  if [[ -f "$file" ]]; then
    python3 - "$file" "$key_path" <<'PY'
import json, sys
path = sys.argv[2].split('.')
data = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
cur = data
for p in path:
    if isinstance(cur, dict) and p in cur:
        cur = cur[p]
    else:
        print("")
        raise SystemExit(0)
print(cur if isinstance(cur, str) else "")
PY
  fi
}

prompt_required() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="${3:-}"
  local value=""

  while [[ -z "$value" ]]; do
    if [[ -n "$default_value" ]]; then
      read -r -p "$prompt_text [${default_value}]: " value
    else
      read -r -p "$prompt_text: " value
    fi

    if [[ -z "$value" && -n "$default_value" ]]; then
      value="$default_value"
    fi

    if [[ -z "$value" ]]; then
      echo "This field is required."
    fi
  done

  printf -v "$var_name" '%s' "$value"
}

run_python_setup_wizard() {
  local wizard="$APP_DIR/install_menu.py"
  if [[ ! -f "$wizard" ]]; then
    die "missing Python setup wizard: $wizard"
  fi

  chmod +x "$wizard"
  python3 "$wizard" --app-dir "$APP_DIR" --config "$CONFIG_FILE"
}

install_global_command() {
  local preferred="/usr/local/bin/servclaw"
  local fallback_dir="$HOME/.local/bin"
  local fallback="$fallback_dir/servclaw"

  if [[ ! -f "$SERVCLAW_CLI_SOURCE" ]]; then
    echo "Error: missing CLI command source at $SERVCLAW_CLI_SOURCE"
    exit 1
  fi

  chmod +x "$SERVCLAW_CLI_SOURCE"

  if [[ -w "/usr/local/bin" ]]; then
    ln -sf "$SERVCLAW_CLI_SOURCE" "$preferred"
    echo "Installed global command: $preferred"
    return
  fi

  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    sudo ln -sf "$SERVCLAW_CLI_SOURCE" "$preferred"
    echo "Installed global command with sudo: $preferred"
    return
  fi

  mkdir -p "$fallback_dir"
  ln -sf "$SERVCLAW_CLI_SOURCE" "$fallback"
  echo "Installed user command: $fallback"

  if [[ ":${PATH}:" != *":$fallback_dir:"* ]]; then
    if [[ -f "$HOME/.profile" ]]; then
      if ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.profile"; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
      fi
    else
      printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"' > "$HOME/.profile"
    fi
    echo "Added $fallback_dir to PATH via ~/.profile (restart shell to pick up)."
  fi
}

echo "== Servclaw Installer =="

require_cmd python3

detect_os_arch
log "Detected OS=$OS_NAME version=$OS_VERSION arch=$ARCH_NORM (raw=$ARCH_RAW)"
log "Package manager=$PKG_MANAGER"

install_docker_and_compose_if_needed
require_cmd docker
if ! docker compose version >/dev/null 2>&1; then
  die "Docker Compose plugin not available after install."
fi
ensure_docker_daemon

# Create workspace and copy template files
WORKSPACE_DIR="$APP_DIR/workspace"
TEMPLATES_DIR="$APP_DIR/templates"

mkdir -p "$APP_DIR/logs" "$APP_DIR/memory" "$WORKSPACE_DIR"

# Copy template MDs to workspace if they don't exist
if [[ -d "$TEMPLATES_DIR" ]]; then
  for template_file in "$TEMPLATES_DIR"/*.md; do
    filename=$(basename "$template_file")
    target="$WORKSPACE_DIR/$filename"
    
    # Only copy if target doesn't exist (preserve user edits)
    if [[ ! -f "$target" ]]; then
      cp "$template_file" "$target"
      log "Copied template: $filename"
    fi
  done
  log "Workspace initialized at $WORKSPACE_DIR"
fi

echo
log "Starting interactive setup wizard..."
run_python_setup_wizard
log "Configuration updated in $CONFIG_FILE"

install_global_command

echo
log "Building and starting container..."
(
  cd "$APP_DIR"
  compose_up
)

echo
log "Setup complete."
echo "Check status:   ${COMPOSE_CMD[*]} ps"
echo "View logs:      ${COMPOSE_CMD[*]} logs -f servclaw"
echo
echo "Authorize Telegram users after they message the bot and get their user ID:"
echo "  servclaw telegram allow <user-id>"

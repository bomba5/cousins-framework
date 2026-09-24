#!/bin/sh
# The image's entrypoint: make the volume a framework root, then exec the
# command (the image's CMD is the supervisor). Runs on every start.
#
#   - the root's directories exist; templates/ is a link into the image,
#     so an upgraded image brings its templates with it;
#   - config/*.example are copied in on every start (the image owns them);
#     of the live names, only config/harness.toml is created, and only
#     when absent: every other example switches a feature on;
#   - an API key passed as the compose secret is installed where the
#     api-key account reads it (a private file of this user), and the
#     account is declared once;
#   - the console is reported OPEN while no console user exists.
#
# FRAMEWORK_ROOT, COUSIN_IMAGE_SRC and COUSIN_API_KEY_SECRET override the
# three paths (the tests run this script on a host).
set -eu

root="${FRAMEWORK_ROOT:-/data}"
src="${COUSIN_IMAGE_SRC:-/opt/framework}"
secret="${COUSIN_API_KEY_SECRET:-/run/secrets/anthropic_api_key}"

say() { printf 'entrypoint: %s\n' "$*" >&2; }
fail() { printf 'entrypoint: %s\n' "$*" >&2; exit 1; }

if [ "$#" -eq 0 ]; then
    printf 'usage: entrypoint.sh <command> [args...]\n' >&2
    exit 2
fi

first_start=no
[ -d "$root/config" ] || first_start=yes

mkdir -p "$root/config" "$root/cousins" "$root/data" "$root/shared" "$root/home"

link="$root/templates"
if [ -L "$link" ]; then
    rm -f "$link"
elif [ -e "$link" ]; then
    fail "$link is a directory, not a link: the image owns templates/ and links it to $src/templates; move yours aside and restart"
fi
ln -s "$src/templates" "$link"

for example in "$src"/config/*.example; do
    [ -f "$example" ] || continue
    cp "$example" "$root/config/"
done
if [ ! -e "$root/config/harness.toml" ] && [ -f "$root/config/harness.toml.example" ]; then
    cp "$root/config/harness.toml.example" "$root/config/harness.toml"
fi

if [ -e "$secret" ]; then
    if [ -r "$secret" ] && [ -s "$secret" ]; then
        (
            umask 077
            mkdir -p "$root/.secrets/accounts"
            chmod 700 "$root/.secrets" "$root/.secrets/accounts"
            cp "$secret" "$root/.secrets/accounts/api-key.tmp"
            chmod 600 "$root/.secrets/accounts/api-key.tmp"
            mv -f "$root/.secrets/accounts/api-key.tmp" "$root/.secrets/accounts/api-key"
        )
        accounts="$root/config/accounts.toml"
        if ! grep -Eq '^[[:space:]]*\[accounts\.("api-key"|api-key)\]' "$accounts" 2>/dev/null; then
            printf '\n[accounts.api-key]\nkind = "anthropic-key"\n' >> "$accounts"
            say "declared [accounts.api-key] in config/accounts.toml"
        fi
    else
        say "$secret is empty or unreadable by uid $(id -u): no key installed." \
            "On the host: chmod 644 secrets/anthropic_api_key (keep secrets/ itself 700)."
    fi
fi

if [ "$first_start" = yes ]; then
    cat >&2 <<EOF
entrypoint: first start on an empty volume ($root). What to edit
  (docker compose exec framework sh, then an editor or a heredoc):
  - config/harness.toml: created from its example; install-wide defaults.
  - config/accounts.toml: how cousins authenticate. The API key lane is the
    compose secret anthropic_api_key (declared here as [accounts.api-key]);
    the login lane is a claude-login account, then:
    docker compose exec framework cousin-account login <name>
  - config/console-users.json: docker compose exec framework cousin-console adduser <name>
  - config/*.example: embeddings, hive, external peers, MCP registry. Copy one
    to its live name to turn that feature on.
EOF
fi

if [ ! -e "$root/config/console-users.json" ]; then
    say "the console is OPEN: config/console-users.json does not exist, so anyone" \
        "who reaches the port is let in. Add a user:" \
        "docker compose exec framework cousin-console adduser <name>"
fi

exec "$@"

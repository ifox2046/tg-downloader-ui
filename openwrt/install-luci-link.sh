#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"

copy_file() {
	src="$SCRIPT_DIR/$1"
	dst="/$1"
	mode="$2"

	if [ ! -f "$src" ]; then
		echo "Missing $src" >&2
		exit 1
	fi

	mkdir -p "$(dirname "$dst")"
	cp "$src" "$dst"
	chmod "$mode" "$dst"
}

copy_file usr/share/luci/menu.d/luci-app-tg-downloader-ui.json 0644
copy_file usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json 0644
copy_file www/luci-static/resources/view/tg-downloader-ui/link.js 0644

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd restart >/dev/null 2>&1 || true
/etc/init.d/uhttpd restart >/dev/null 2>&1 || true

echo "Installed LuCI link: Services -> Telegram Downloads"

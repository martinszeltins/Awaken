#!/usr/bin/env bash
set -u

if (( $# != 1 )) || [[ -z ${1:-} ]]; then
    printf 'Usage: %s APP_NAME\nExample: %s netbird\n' "$0" "$0" >&2
    exit 2
fi

search_term=$1
search_term_lower=${search_term,,}
match_count=0


check_xdg_autostart() {
    local previous_count=$match_count
    local directory file entry
    local -a directories=("${XDG_CONFIG_HOME:-$HOME/.config}/autostart")
    local -a system_config_dirs

    IFS=: read -r -a system_config_dirs <<< "${XDG_CONFIG_DIRS:-/etc/xdg}"
    for directory in "${system_config_dirs[@]}"; do
        directories+=("$directory/autostart")
    done
    directories+=("/usr/share/gnome/autostart")

    printf '[info] Checking XDG / GNOME autostart...\n'
    for directory in "${directories[@]}"; do
        [[ -d $directory ]] || continue

        while IFS= read -r -d '' file; do
            [[ -f $file ]] || continue
            # Match filenames too, so an entry is found even if its Exec differs.
            entry=${file##*/}
            if [[ ${entry,,} == *"$search_term_lower"* ]] || grep -qiF -- "$search_term" "$file" 2>/dev/null; then
                printf '[found] Desktop entry: %s\n' "$file"
                if [[ -L $file ]]; then
                    printf '        Links to: %s\n' "$(readlink -f -- "$file")"
                fi
                grep -E '^(Exec|Hidden|TryExec|OnlyShowIn|NotShowIn|X-GNOME-Autostart-enabled)=' "$file" 2>/dev/null | sed 's/^/        /' || true
                ((match_count += 1))
            fi
        done < <(find "$directory" -maxdepth 1 \( -type f -o -type l \) -iname '*.desktop' -print0 2>/dev/null)
    done

    if (( match_count == previous_count )); then
        printf '[info] Not found\n'
    fi
}

check_systemd() {
    local scope=$1
    local previous_count=$match_count
    local directory file filename state active
    local -a manager_args=()
    local -a analyzer_args=()
    declare -A seen=()

    if [[ $scope == user ]]; then
        manager_args=(--user)
        analyzer_args=(--user)
    fi

    printf '[info] Checking systemd %s units...\n' "$scope"

    # Ask systemd for its own unit search paths instead of hardcoding them.
    while IFS= read -r directory; do
        [[ -d $directory ]] || continue

        while IFS= read -r -d '' file; do
            [[ -f $file ]] || continue
            [[ -v seen["$file"] ]] && continue
            seen["$file"]=1

            filename=${file##*/}
            if [[ ${filename,,} != *"$search_term_lower"* ]] && ! grep -qiF -- "$search_term" "$file" 2>/dev/null; then
                continue
            fi

            printf '[found] %s unit file: %s\n' "$scope" "$file"
            if [[ $filename != *.conf ]]; then
                state=$(systemctl "${manager_args[@]}" is-enabled "$filename" 2>/dev/null || true)
                active=$(systemctl "${manager_args[@]}" is-active "$filename" 2>/dev/null || true)
                printf '        Unit: %s | enabled: %s | active: %s\n' \
                    "$filename" "${state:-unknown}" "${active:-unknown}"
            else
                printf '        (systemd override / drop-in)\n'
            fi
            ((match_count += 1))
        done < <(find "$directory" -maxdepth 3 \( -type f -o -type l \) \
            \( -name '*.service' -o -name '*.timer' -o -name '*.socket' -o -name '*.path' -o -name '*.conf' \) \
            -print0 2>/dev/null)
    done < <(systemd-analyze "${analyzer_args[@]}" unit-paths 2>/dev/null)

    if (( match_count == previous_count )); then
        printf '[info] Not found\n'
    fi
}

printf '[info] Searching startup configurations for: %s\n\n' "$search_term"
check_xdg_autostart
printf '\n'
check_systemd system
printf '\n'
check_systemd user
printf '\n'

if (( match_count == 0 )); then
    printf '[info] No matching XDG autostart entries or systemd unit files found.\n'
    printf '[info] Other possibilities: application-specific launchers, cron, shell scripts, containers.\n'
else
    printf '[info] %d matching file(s). A match does not necessarily mean it auto-starts.\n' "$match_count"
fi

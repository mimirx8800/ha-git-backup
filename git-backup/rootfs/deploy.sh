#!/usr/bin/env bash
# Safe, manual Git -> Home Assistant deploy helper.
# Sourced by /run.sh after add-on configuration has been loaded.

DEPLOY_STATUS_FILE="/data/deploy_status.json"

update_deploy_status() {
    local status="$1"
    local message="$2"
    local files_json="${3:-[]}"
    local blocked_json="${4:-[]}"
    local conflicts_json="${5:-[]}"
    local base_commit="${6:-}"
    local deploy_commit="${7:-}"

    jq -n \
        --arg status "$status" \
        --arg message "$message" \
        --arg branch "$DEPLOY_BRANCH" \
        --arg base_commit "$base_commit" \
        --arg deploy_commit "$deploy_commit" \
        --argjson files "$files_json" \
        --argjson blocked "$blocked_json" \
        --argjson conflicts "$conflicts_json" \
        '{
            status: $status,
            message: $message,
            branch: $branch,
            files: $files,
            blocked: $blocked,
            conflicts: $conflicts,
            base_commit: $base_commit,
            deploy_commit: $deploy_commit,
            last_update: (now | todate)
        }' > "$DEPLOY_STATUS_FILE"
}

init_deploy_status() {
    if [ "$DEPLOY_ENABLED" = "true" ]; then
        update_deploy_status "idle" "ChatGPT deploy enabled; preview changes before applying"
    else
        update_deploy_status "disabled" "ChatGPT deploy is disabled in add-on configuration"
    fi
}

lines_to_json_array() {
    local value="${1:-}"
    if [ -z "$value" ]; then
        echo '[]'
    else
        printf '%s\n' "$value" | sed '/^$/d' | jq -R . | jq -s .
    fi
}

is_allowed_deploy_file() {
    case "$1" in
        automations.yaml|scripts.yaml|scenes.yaml)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

build_deploy_plan() {
    if [ "$DEPLOY_ENABLED" != "true" ]; then
        update_deploy_status "disabled" "ChatGPT deploy is disabled in add-on configuration"
        return 1
    fi

    cd "$REPO_DIR"

    if ! git fetch --prune origin "$BRANCH" "$DEPLOY_BRANCH" >/dev/null 2>&1; then
        update_deploy_status "error" "Could not fetch main/deploy branches from Git"
        return 1
    fi

    local main_ref="origin/$BRANCH"
    local deploy_ref="origin/$DEPLOY_BRANCH"

    if ! git rev-parse --verify "$main_ref" >/dev/null 2>&1; then
        update_deploy_status "error" "Main backup branch is unavailable"
        return 1
    fi
    if ! git rev-parse --verify "$deploy_ref" >/dev/null 2>&1; then
        update_deploy_status "error" "Deploy branch '$DEPLOY_BRANCH' does not exist"
        return 1
    fi

    local base_commit
    base_commit=$(git merge-base "$main_ref" "$deploy_ref" 2>/dev/null || true)
    if [ -z "$base_commit" ]; then
        update_deploy_status "error" "Could not determine a common Git base for the deploy branch"
        return 1
    fi

    local deploy_commit
    deploy_commit=$(git rev-parse "$deploy_ref")

    local changed
    changed=$(git diff --name-only "$base_commit" "$deploy_ref" -- 2>/dev/null || true)

    if [ -z "$changed" ]; then
        update_deploy_status "no_changes" "No ChatGPT configuration changes are pending" "[]" "[]" "[]" "$base_commit" "$deploy_commit"
        return 2
    fi

    local pending=""
    local blocked=""
    local conflicts=""

    while IFS= read -r file; do
        [ -z "$file" ] && continue

        if ! is_allowed_deploy_file "$file"; then
            blocked+="${blocked:+$'\n'}$file"
            continue
        fi

        # Deletion is deliberately blocked in v1.1.0.
        if ! git cat-file -e "$deploy_ref:$file" 2>/dev/null; then
            blocked+="${blocked:+$'\n'}$file (deletion blocked)"
            continue
        fi

        local base_blob=""
        local main_blob=""
        local deploy_blob=""
        base_blob=$(git rev-parse "$base_commit:$file" 2>/dev/null || true)
        main_blob=$(git rev-parse "$main_ref:$file" 2>/dev/null || true)
        deploy_blob=$(git rev-parse "$deploy_ref:$file" 2>/dev/null || true)

        # If the exact candidate is already on main, it has already been applied.
        if [ -n "$main_blob" ] && [ "$main_blob" = "$deploy_blob" ]; then
            continue
        fi

        # Refuse stale proposals instead of overwriting a newer HA edit.
        if [ "$base_blob" != "$main_blob" ]; then
            conflicts+="${conflicts:+$'\n'}$file"
            continue
        fi

        pending+="${pending:+$'\n'}$file"
    done <<< "$changed"

    local pending_json blocked_json conflicts_json
    pending_json=$(lines_to_json_array "$pending")
    blocked_json=$(lines_to_json_array "$blocked")
    conflicts_json=$(lines_to_json_array "$conflicts")

    if [ -n "$blocked" ] || [ -n "$conflicts" ]; then
        update_deploy_status "blocked" "Deploy blocked: forbidden files or a newer Home Assistant edit was detected" \
            "$pending_json" "$blocked_json" "$conflicts_json" "$base_commit" "$deploy_commit"
        return 1
    fi

    if [ -z "$pending" ]; then
        update_deploy_status "no_changes" "This ChatGPT proposal is already applied" "[]" "[]" "[]" "$base_commit" "$deploy_commit"
        return 2
    fi

    update_deploy_status "ready" "ChatGPT changes are ready for manual apply" \
        "$pending_json" "[]" "[]" "$base_commit" "$deploy_commit"
    return 0
}

homeassistant_config_check() {
    if ! load_supervisor_token; then
        echo "Supervisor token unavailable"
        return 1
    fi

    local response
    if ! response=$(curl -fsS -X POST \
        -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{}' \
        "http://supervisor/core/api/config/core/check_config" 2>&1); then
        echo "$response"
        return 1
    fi

    local result
    result=$(printf '%s' "$response" | jq -r '.result // "unknown"' 2>/dev/null || echo "unknown")
    if [ "$result" = "valid" ]; then
        return 0
    fi

    printf '%s' "$response" | jq -r '.errors // "Home Assistant reported an invalid configuration"' 2>/dev/null || \
        echo "Home Assistant reported an invalid configuration"
    return 1
}

homeassistant_reload_domain() {
    local domain="$1"

    if ! load_supervisor_token; then
        return 1
    fi

    curl -fsS -X POST \
        -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{}' \
        "http://supervisor/core/api/services/${domain}/reload" >/dev/null 2>&1
}

rollback_deploy_files() {
    local backup_dir="$1"
    local files_json="$2"

    while IFS= read -r file; do
        [ -z "$file" ] && continue
        if [ -f "$backup_dir/$file" ]; then
            cp -p "$backup_dir/$file" "$HA_CONFIG/$file"
        elif [ -f "$backup_dir/.missing-$file" ]; then
            rm -f "$HA_CONFIG/$file"
        fi
    done < <(printf '%s' "$files_json" | jq -r '.[]')
}

do_deploy_preview() {
    log_info "Refreshing ChatGPT deploy preview..."
    if build_deploy_plan; then
        log_info "ChatGPT deploy preview is ready"
        return 0
    else
        local rc=$?
        [ "$rc" -eq 2 ] && return 0
        return "$rc"
    fi
}

do_deploy_apply() {
    if [ "$DEPLOY_ENABLED" != "true" ]; then
        update_deploy_status "disabled" "ChatGPT deploy is disabled in add-on configuration"
        return 1
    fi

    log_info "Starting safe ChatGPT configuration deploy..."
    update_deploy_status "running" "Creating a fresh backup before applying ChatGPT changes..."

    # The pre-deploy backup makes main an exact view of current HA config.
    if ! do_backup "pre_deploy"; then
        update_deploy_status "error" "Pre-deploy backup failed; nothing was changed"
        return 1
    fi

    if build_deploy_plan; then
        :
    else
        local plan_rc=$?
        [ "$plan_rc" -eq 2 ] && return 0
        return "$plan_rc"
    fi

    local files_json
    files_json=$(jq -c '.files // []' "$DEPLOY_STATUS_FILE")
    local file_count
    file_count=$(printf '%s' "$files_json" | jq 'length')

    if [ "$file_count" -eq 0 ]; then
        update_deploy_status "no_changes" "No deployable changes remain"
        return 0
    fi

    local timestamp
    timestamp=$(date '+%Y%m%d-%H%M%S')
    local backup_dir="/data/deploy_backups/$timestamp"
    local candidate_dir="/data/deploy_candidates/$timestamp"
    mkdir -p "$backup_dir" "$candidate_dir"

    cd "$REPO_DIR"

    # Save rollback copies and extract candidates before touching /config.
    while IFS= read -r file; do
        [ -z "$file" ] && continue

        if [ -f "$HA_CONFIG/$file" ]; then
            cp -p "$HA_CONFIG/$file" "$backup_dir/$file"
        else
            touch "$backup_dir/.missing-$file"
        fi

        if ! git show "origin/$DEPLOY_BRANCH:$file" > "$candidate_dir/$file"; then
            update_deploy_status "error" "Could not extract candidate file: $file" "$files_json"
            return 1
        fi

        local size
        size=$(stat -c%s "$candidate_dir/$file" 2>/dev/null || echo 0)
        if [ "$size" -gt $((2 * 1024 * 1024)) ]; then
            update_deploy_status "blocked" "Candidate is unexpectedly large: $file" "$files_json"
            return 1
        fi

        if grep -Eaq 'github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' \
            "$candidate_dir/$file" 2>/dev/null; then
            update_deploy_status "blocked" "Credential-like content detected in candidate: $file" "$files_json"
            return 1
        fi
    done < <(printf '%s' "$files_json" | jq -r '.[]')

    # Atomic per-file replacement.
    while IFS= read -r file; do
        [ -z "$file" ] && continue
        local tmp_target="$HA_CONFIG/.chatgpt-deploy-$file.$$"
        cp "$candidate_dir/$file" "$tmp_target"
        chmod 0644 "$tmp_target" 2>/dev/null || true
        mv -f "$tmp_target" "$HA_CONFIG/$file"
    done < <(printf '%s' "$files_json" | jq -r '.[]')

    if [ "$DEPLOY_REQUIRE_CONFIG_CHECK" = "true" ]; then
        local check_error=""
        if ! check_error=$(homeassistant_config_check); then
            log_error "Home Assistant configuration check failed; rolling back"
            rollback_deploy_files "$backup_dir" "$files_json"
            homeassistant_config_check >/dev/null 2>&1 || true
            update_deploy_status "rolled_back" "Home Assistant rejected the configuration; original files were restored. ${check_error:0:500}" "$files_json"
            return 1
        fi
    fi

    local reload_warning=""
    if [ "$DEPLOY_AUTO_RELOAD" = "true" ]; then
        local needs_automation=false
        local needs_script=false
        local needs_scene=false

        while IFS= read -r file; do
            case "$file" in
                automations.yaml) needs_automation=true ;;
                scripts.yaml) needs_script=true ;;
                scenes.yaml) needs_scene=true ;;
            esac
        done < <(printf '%s' "$files_json" | jq -r '.[]')

        if [ "$needs_automation" = "true" ] && ! homeassistant_reload_domain "automation"; then
            reload_warning="automation reload failed"
        fi
        if [ "$needs_script" = "true" ] && ! homeassistant_reload_domain "script"; then
            reload_warning="${reload_warning:+$reload_warning; }script reload failed"
        fi
        if [ "$needs_scene" = "true" ] && ! homeassistant_reload_domain "scene"; then
            reload_warning="${reload_warning:+$reload_warning; }scene reload failed"
        fi
    fi

    # Preserve the accepted configuration in the normal main backup history.
    if ! do_backup "post_deploy"; then
        update_deploy_status "warning" "Files were applied and validated, but the post-deploy Git backup failed" "$files_json"
        return 0
    fi

    if [ -n "$reload_warning" ]; then
        update_deploy_status "warning" "Files were applied and validated; reload warning: $reload_warning" "$files_json"
    else
        update_deploy_status "success" "ChatGPT changes were applied, validated, reloaded and backed up" "$files_json"
    fi

    log_info "Safe ChatGPT deploy completed"
    return 0
}
